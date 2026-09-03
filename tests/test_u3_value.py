"""U3 tests — the Value Verdict (03-engine §5.2), THE Buy differentiator.

DETERMINISTIC: never hits the network. The real-data tests read the on-disk
``data/comps_enriched.json`` cache (the date-vetted HM Land Registry + EPC comps
built by U9/EPC) and the golden De Beauvoir subject; synthetic-comp tests use
in-file light dicts. Reproducible on every run.

Runnable two ways (matching tests/test_u9_landreg.py):

    python3 -m pytest tests/test_u3_value.py -v     # if pytest is installed
    python3 tests/test_u3_value.py                  # plain-stdlib fallback
"""

import os
import sys

# Make the repo root importable whether run by pytest (from root) or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.value import (  # noqa: E402
    CONFIG, Adjustment, _llf_trusted, adjustments, fair_estimate,
    lease_adjustment, lease_extension_cost, load_enriched_comps, qualifies_llf,
    select_anchor, value_confidence, value_score, value_verdict, verdict_tag,
)
from gaff_engine.schemas import ValueBand, ValueTag, ValueVerdict  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING  # noqa: E402


# ---------------------------------------------------------------------------
# Light synthetic builders (the accessors read Comp / Listing / dict alike).
# ---------------------------------------------------------------------------

def make_comp(ppsf, conf="high", same_street=True, gap=0.5, price=None,
              ptype="flat-maisonette", tid=None, category=None):
    comp = {
        "pricePerSqft": ppsf,
        "areaConfidence": conf,
        "distanceNote": "same street" if same_street else "De Beauvoir Road",
        "epcSaleGapYears": gap,
        "price": int(price if price is not None else ppsf * 1000),
        "date": "2025-06-01",
        "propertyType": ptype,
        "tenure": "Leasehold",
        "transactionId": tid or ("T-%s-%s" % (int(ppsf), conf)),
        "address": {"paon": "1", "street": "NORTHCHURCH ROAD", "postcode": "N1 3NT"},
    }
    # category None = key absent (an enriched set built before the field was
    # captured); "standard"/"additional" = a parse_comp-captured PPD category.
    if category is not None:
        comp["transactionCategory"] = category
    return comp


def make_subject(sqft=1000, ask=1_000_000, lease=89, tenure="leasehold",
                 ptype="maisonette"):
    return {
        "sqft": sqft,
        "propertyType": ptype,
        "buy": {"price": {"amount": ask},
                "tenure": {"type": tenure, "leaseYearsRemaining": lease}},
    }


def _load_real_anchor():
    comps = load_enriched_comps()
    anchor, label = select_anchor(comps, GOLDEN_LISTING)
    return comps, anchor, label


# ---------------------------------------------------------------------------
# 1 · Fair estimate on the REAL same-street trusted comps.
# ---------------------------------------------------------------------------

def test_fair_estimate_real_same_street_in_sane_range():
    """Median £/sqft x 1050 sqft on the real same-street trusted comps lands in a
    sane £0.9m-1.3m range (design mandate), with the expected 4-comp anchor."""
    _, anchor, label = _load_real_anchor()
    assert len(anchor) == 4, "real same-street {high,medium} anchor is 4 comps"
    fe = fair_estimate(anchor, GOLDEN_LISTING.sqft)
    assert 900_000 <= fe["estimate"] <= 1_300_000, fe["estimate"]
    assert fe["n"] == 4
    # £1,177/sqft on the 4 trusted same-street comps (the design-mandate example).
    assert 1150 <= fe["ppsfMedian"] <= 1200, fe["ppsfMedian"]
    assert fe["band"].low < fe["estimate"] < fe["band"].high


# ---------------------------------------------------------------------------
# 2 · Anchor selection — prefers same-street {high,medium}; widens when thin.
# ---------------------------------------------------------------------------

def test_anchor_prefers_same_street_trusted():
    _, anchor, label = _load_real_anchor()
    assert label.startswith("same-street trusted")
    assert all(c.distanceNote == "same street" for c in anchor)
    assert all(c.areaConfidence in ("high", "medium") for c in anchor)


def test_anchor_widens_to_area_when_same_street_thin():
    """<3 same-street trusted comps but >=3 area trusted -> widen to the area."""
    subject = make_subject()
    comps = (
        [make_comp(1200, conf="high", same_street=True, tid="ss1"),
         make_comp(1220, conf="medium", same_street=True, tid="ss2")]   # only 2 same-street
        + [make_comp(1100, conf="high", same_street=False, tid="a%d" % i) for i in range(4)]
    )
    anchor, label = select_anchor(comps, subject)
    assert label.startswith("area trusted"), label
    assert len(anchor) == 6


def test_anchor_falls_to_all_matched_when_trusted_thin():
    """<3 trusted anywhere -> all matched comps, confidence capped low."""
    subject = make_subject()
    comps = (
        [make_comp(1200, conf="high", same_street=True, tid="ss1")]      # 1 trusted only
        + [make_comp(900 + i, conf="low", same_street=(i % 2 == 0), tid="l%d" % i)
           for i in range(5)]
    )
    anchor, label = select_anchor(comps, subject)
    assert "all matched comps" in label, label
    assert len(anchor) == 6


# ---------------------------------------------------------------------------
# 3 · Tag logic — synthetic deltas against the config thresholds (+ comp gate).
# ---------------------------------------------------------------------------

def test_tag_thresholds_with_sufficient_comps():
    steal, over = CONFIG["stealPct"], CONFIG["overPct"]
    assert steal == -5.0 and over == 5.0                 # the thresholds we test against
    tag, capped = verdict_tag(-8.0, n_comps=6)
    assert tag == ValueTag.STEAL and not capped          # well under fair -> steal
    tag, capped = verdict_tag(+8.0, n_comps=6)
    assert tag == ValueTag.OVER and not capped           # well over -> over
    tag, capped = verdict_tag(-1.0, n_comps=6)
    assert tag == ValueTag.FAIR and not capped           # near fair -> fair
    # exact boundaries: <= -5 steal, >= +5 over.
    assert verdict_tag(-5.0, 6)[0] == ValueTag.STEAL
    assert verdict_tag(5.0, 6)[0] == ValueTag.OVER
    assert verdict_tag(-4.9, 6)[0] == ValueTag.FAIR


def test_comp_sufficiency_gate_caps_and_forces_fair():
    """3-4 comps cap a non-fair tag to fair; <3 forces fair (§5.2d, A6)."""
    tag, capped = verdict_tag(-8.0, n_comps=4)           # steal raw, only 4 comps
    assert tag == ValueTag.FAIR and capped
    tag, capped = verdict_tag(-8.0, n_comps=2)           # forced fair, <3
    assert tag == ValueTag.FAIR and capped
    tag, capped = verdict_tag(+9.0, n_comps=4)           # over raw, capped to fair
    assert tag == ValueTag.FAIR and capped


# ---------------------------------------------------------------------------
# 4 · Lease adjustment — the marriage-value model + the golden mechanism.
# ---------------------------------------------------------------------------

def test_lease_cost_curve_negligible_modest_cliff():
    V = 1_000_000
    assert lease_extension_cost(990, V) == 0.0           # ~zero far above 90
    assert lease_extension_cost(90, V) == 0.0            # zero at the 90 line
    c89 = lease_extension_cost(89, V)
    c70 = lease_extension_cost(70, V)
    assert 0 < c89 < 0.02 * V                            # 89-yr: small/modest
    assert c70 > 8 * c89                                 # 70-yr: much larger (cliff)
    # monotonic: shorter lease never costs less (years descending -> cost ascending).
    years = [990, 95, 90, 89, 85, 80, 78, 70, 60]
    costs = [lease_extension_cost(y, V) for y in years]
    assert costs == sorted(costs), costs


def test_lease_adjustment_89yr_small_990yr_none():
    small = lease_adjustment(make_subject(lease=89, ask=1_150_000))
    assert isinstance(small, Adjustment) and small.kind == "lease_adj"
    assert small.direction == "worsens" and small.isEstimate
    assert 0 < small.amountGBP < 30_000                  # modest (golden is 89-yr)
    big = lease_adjustment(make_subject(lease=70, ask=1_150_000))
    assert big.amountGBP > 5 * small.amountGBP           # marriage-value cliff
    assert lease_adjustment(make_subject(lease=990, ask=1_150_000)) is None
    # a share-of-freehold subject gets no lease-extension cost.
    assert lease_adjustment(make_subject(lease=None, tenure="share_of_freehold")) is None


def test_headline_steal_pulled_to_fair_by_lease_alone():
    """The golden mechanism, COMPUTED: with enough comps (no comp gate), a short
    lease alone drags a headline 'steal' to an honest 'fair' (§5.2c, A4)."""
    # 6 tight same-street trusted comps at £1,000/sqft, 1000 sqft -> fair £1,000,000.
    comps = [make_comp(1000, conf="high", same_street=True, gap=0.5, tid="t%d" % i)
             for i in range(6)]
    subject = make_subject(sqft=1000, ask=925_000, lease=83)   # headline -7.5% (steal)
    v = value_verdict(subject, comps)
    assert v.headlineDeltaPct <= CONFIG["stealPct"], v.headlineDeltaPct  # looks a steal
    assert verdict_tag(v.headlineDeltaPct, n_comps=6)[0] == ValueTag.STEAL
    assert v.deltaPct > v.headlineDeltaPct                # lease worsened the delta upward
    assert v.tag == ValueTag.FAIR                         # ...into fair territory
    assert CONFIG["stealPct"] < v.deltaPct < CONFIG["overPct"]
    # and the flip is sourced to a lease Adjustment evidence item.
    assert any(e.kind == "lease_adj" for e in v.evidence)


# ---------------------------------------------------------------------------
# 5 · Confidence — count up, spread/staleness down (§5.8).
# ---------------------------------------------------------------------------

def test_confidence_4_comp_tight_reads_medium():
    """The real 4-comp same-street anchor reads MEDIUM, not high (design mandate)."""
    _, anchor, _ = _load_real_anchor()
    conf = value_confidence(anchor)
    assert conf["band"] == "medium", conf
    assert 0.5 <= conf["scalar"] < 0.75, conf["scalar"]


def test_confidence_15_comp_tight_reads_high():
    tight = [make_comp(1000 + (i % 5 - 2) * 5, conf="high", gap=0.5, tid="h%d" % i)
             for i in range(15)]                          # £990-1010/sqft, tiny spread
    conf = value_confidence(tight)
    assert conf["band"] == "high", conf
    assert conf["scalar"] >= 0.75


def test_confidence_wide_spread_reads_lower():
    tight = [make_comp(1000 + (i % 5 - 2) * 5, conf="high", gap=0.5, tid="h%d" % i)
             for i in range(15)]
    wide = [make_comp(500 + i * 70, conf="high", gap=0.5, tid="w%d" % i)
            for i in range(15)]                           # £500-1480/sqft, big spread
    c_tight = value_confidence(tight)
    c_wide = value_confidence(wide)
    assert c_wide["scalar"] < c_tight["scalar"]           # spread pulls confidence down
    assert c_wide["band"] != "high"


# ---------------------------------------------------------------------------
# 6 · Schema-valid ValueVerdict on the real golden subject (+ the mechanism).
# ---------------------------------------------------------------------------

def test_value_verdict_is_schema_valid():
    comps = load_enriched_comps()
    v = value_verdict(GOLDEN_LISTING, comps)
    assert isinstance(v, ValueVerdict)
    assert validate(v) == []                              # contract-clean
    assert isinstance(v.tag, ValueTag)
    assert isinstance(v.band, ValueBand) and v.band.low < v.band.high
    assert 0.0 <= v.position <= 1.0
    assert v.fairEstimate > 0 and v.streetMedianPerSqft > 0
    # every comp evidence item is sourced (§5.7 rule 3: compRef present).
    comp_ev = [e for e in v.evidence if e.kind == "comp"]
    assert comp_ev and all(e.compRef is not None for e in comp_ev)
    assert v.score == value_score(v)                      # score recomputes from deltaPct


def test_real_de_beauvoir_golden_mechanism():
    """The real £1.15m De Beauvoir verdict: a headline steal, an honest fair, at
    medium confidence — the 'looks like a steal, actually fair' story, computed."""
    comps = load_enriched_comps()
    v = value_verdict(GOLDEN_LISTING, comps)
    assert v.headlineDeltaPct <= CONFIG["stealPct"]       # headline looks a steal
    assert v.tag == ValueTag.FAIR                         # ...but the verdict is fair
    assert "capped to fair" in v.basis                    # via the comp-sufficiency gate
    assert 0.5 <= v.confidence < 0.75                     # medium, honest
    assert any(e.kind == "lease_adj" for e in v.evidence)  # the 89-yr lease is sourced
    assert 1 <= len(v.reasons) <= 3 and all(isinstance(r, str) for r in v.reasons)


# ---------------------------------------------------------------------------
# Fail-soft (A2): thin real stock must never crash the verdict.
# ---------------------------------------------------------------------------

def test_verdict_missing_sqft_falls_back_to_like_for_like():
    """A real listing with no floor area but qualifying comps no longer dead-ends
    in NEEDS_DATA: §5.2b's like-for-like fallback prices it against whole sold
    prices, labelled plainly, with no fabricated £/sqft. Still never raises."""
    import dataclasses
    no_sqft = dataclasses.replace(GOLDEN_LISTING, sqft=None)
    v = value_verdict(no_sqft, load_enriched_comps())          # must not raise
    assert v.tag != ValueTag.NEEDS_DATA                        # a real verdict now
    assert "like-for-like" in v.basis and "no floor area" in v.basis.lower()
    assert v.fairEstimate > 0 and v.deltaPct is not None
    assert v.streetMedianPerSqft is None                       # honestly absent: no £/sqft
    assert v.reasons and "floor area" in v.reasons[0].lower()
    assert validate(v) == []


def test_verdict_fails_soft_on_missing_sqft_and_no_comps():
    """No floor area AND no comps at all is still the honest NEEDS_DATA
    empty-state (fail-soft, A2): no numbers, zero confidence, a plain reason."""
    import dataclasses
    no_sqft = dataclasses.replace(GOLDEN_LISTING, sqft=None)
    v = value_verdict(no_sqft, [])                             # must not raise
    assert v.tag == ValueTag.NEEDS_DATA
    assert v.deltaPct is None and v.fairEstimate is None and v.band is None
    assert v.confidence == 0.0 and v.reasons and "floor area" in v.reasons[0].lower()
    assert validate(v) == []                                    # null-on-empty-state is valid


def test_verdict_fails_soft_on_no_asking_price_or_comps():
    """No asking price, or no qualifying comps, also yield NEEDS_DATA not a raise."""
    # A subject with a floor area but no asking price (accessors read dict or dataclass).
    no_ask = value_verdict({"sqft": 1000}, load_enriched_comps())
    assert no_ask.tag == ValueTag.NEEDS_DATA and "asking price" in no_ask.reasons[0].lower()
    # No qualifying comps -> the honest "nothing to price against" empty-state.
    no_comps = value_verdict(GOLDEN_LISTING, [])
    assert no_comps.tag == ValueTag.NEEDS_DATA and "comparable" in no_comps.reasons[0].lower()


# ---------------------------------------------------------------------------
# 7 · No-sqft like-for-like fallback (§5.2b Fair_llf) — synthetic comps only.
#     Comp dates sit at the HPI as-of month so no network is ever touched.
# ---------------------------------------------------------------------------

def _llf_comps(price=1_000_000, n=6, ptype="flat-maisonette"):
    return [make_comp(1000, conf="high", same_street=True, gap=0.5,
                      price=price, ptype=ptype, tid="llf%d" % i)
            for i in range(n)]


def test_llf_fallback_prices_a_no_sqft_subject():
    """No sqft + qualifying comps -> a real like-for-like verdict from SOLD
    PRICES: fair estimate = median sold price, plain basis, no invented £/sqft."""
    subject = make_subject(sqft=None, ask=1_000_000, lease=990)   # no lease adj
    v = value_verdict(subject, _llf_comps())
    assert v.tag == ValueTag.FAIR
    assert v.fairEstimate == 1_000_000                  # median of six £1m sales
    assert v.headlineDeltaPct == 0.0 and v.deltaPct == 0.0
    assert v.streetMedianPerSqft is None                # no £/sqft exists here
    assert "like-for-like" in v.basis and "no floor area" in v.basis.lower()
    assert v.score == value_score(v)
    comp_ev = [e for e in v.evidence if e.kind == "comp"]
    assert comp_ev and all(e.compRef is not None for e in comp_ev)
    assert validate(v) == []


def test_llf_confidence_dampened_one_level():
    """The llf read is coarser than £/sqft, so a would-be HIGH confidence set
    (6 identical-price trusted same-street comps) reads MEDIUM."""
    comps = _llf_comps()
    subject = make_subject(sqft=None, ask=1_000_000, lease=990)
    undampened = value_confidence(comps)["scalar"]
    assert undampened >= 0.75                           # would read high on £/sqft
    v = value_verdict(subject, comps)
    assert v.confidence < undampened
    assert 0.5 <= v.confidence < 0.75                   # one level down -> medium
    assert "dampened" in v.basis


def test_llf_tag_and_comp_gate_still_apply():
    """The llf delta drives the same steal/fair/over tag + comp-sufficiency gate."""
    subject = make_subject(sqft=None, ask=900_000, lease=990)     # -10% vs £1m
    v = value_verdict(subject, _llf_comps(n=6))
    assert v.tag == ValueTag.STEAL and v.deltaPct == -10.0
    capped = value_verdict(subject, _llf_comps(n=3))              # thin set -> gated
    assert capped.tag == ValueTag.FAIR and "capped to fair" in capped.basis


def test_llf_lease_adjustment_still_applies():
    """A short lease worsens the llf delta exactly as on the £/sqft path."""
    subject = make_subject(sqft=None, ask=925_000, lease=83)
    v = value_verdict(subject, _llf_comps(n=6))
    assert v.deltaPct > v.headlineDeltaPct
    assert any(e.kind == "lease_adj" for e in v.evidence)


def test_llf_wrong_class_comps_yield_honest_needs_data():
    """A no-sqft HOUSE subject over all-flat comps: NEEDS_DATA, but the reason
    admits the sales exist and names the blocker, never 'no sales nearby'."""
    subject = make_subject(sqft=None, ptype="terraced", lease=990)
    v = value_verdict(subject, _llf_comps(ptype="flat-maisonette", n=5))
    assert v.tag == ValueTag.NEEDS_DATA
    r = v.reasons[0]
    assert "5 sales exist" in r and "none are comparable" in r
    assert "wrong property class" in r and "terraced" in r
    assert "unlock" in r.lower()
    assert "no comparable sales nearby" not in r.lower()


def test_llf_anomalous_low_prices_yield_needs_data_not_crash():
    """£1-class HM Land Registry transfer records can drag the llf median under
    £500, which _round_pounds collapses to £0 — that must surface as the honest
    NEEDS_DATA empty-state (fail-soft, A2), never a ZeroDivisionError."""
    subject = make_subject(sqft=None, ask=500_000, lease=990)
    comps = [make_comp(None, price=p, tid="anom%d" % i)
             for i, p in enumerate((400, 450, 420))]
    v = value_verdict(subject, comps)                    # must not raise
    assert v.tag == ValueTag.NEEDS_DATA
    assert v.fairEstimate is None and v.deltaPct is None and v.band is None
    assert v.confidence == 0.0
    assert "anomalous" in v.reasons[0].lower()           # names the real blocker
    assert validate(v) == []


def test_llf_single_comp_band_is_nonzero_and_gauge_moves():
    """One qualifying comp gives a point raw band; §5.2b promises a symmetric
    interval whose half-width grows with (1 - valueConfidence), so the
    published band must be nonzero and the gauge must respond to the ask —
    not pin a meaningless centred needle."""
    comp = [make_comp(None, price=480_000, tid="solo")]
    hi = value_verdict(make_subject(sqft=None, ask=500_000, lease=990), comp)
    lo = value_verdict(make_subject(sqft=None, ask=300_000, lease=990), comp)
    assert hi.fairEstimate == 480_000
    assert hi.band.low < hi.fairEstimate < hi.band.high  # nonzero width
    assert hi.position != lo.position                    # the needle moves
    assert validate(hi) == []


def test_llf_identical_price_comps_still_get_a_nonzero_band():
    """Six identical sold prices give a zero-width IQR — the same degenerate
    band as the single-comp case, and the same nonzero-band promise applies."""
    v = value_verdict(make_subject(sqft=None, ask=1_000_000, lease=990),
                      _llf_comps(n=6))
    assert v.band.low < v.fairEstimate < v.band.high
    assert validate(v) == []


def test_llf_same_street_raw_sales_anchor_tier_one():
    """L2 fixer pass, pinned: raw Land Registry sales (areaConfidence None —
    no EPC match) from the subject's own street must anchor the llf read.
    The £/sqft trust gate (areaConfidence high/medium) measures floor-area
    trust, irrelevant to a whole-price read; gating llf tiers on it made the
    declared strongest tier unreachable for every user-warmed street — the
    whole pool of raw sales the llf path exists to use."""
    subject = make_subject(sqft=None, ask=550_000, lease=990)
    street = [make_comp(None, conf=None, same_street=True, gap=0.0,
                        price=544_000, tid="raw%d" % i) for i in range(5)]
    area = [make_comp(1000, conf="high", same_street=False,
                      price=625_000, tid="epc%d" % i) for i in range(6)]
    anchor, label = select_anchor(street + area, subject,
                                  qualifier=qualifies_llf, trusted=_llf_trusted)
    assert label == "same-street sold prices"
    assert len(anchor) == 5                              # the street's own sales
    v = value_verdict(subject, street + area)
    assert v.fairEstimate == 544_000                     # anchored on the street
    assert "same-street sold prices" in v.basis
    assert "areaConfidence" not in v.basis               # never cited by an llf read
    assert validate(v) == []


def test_single_sale_needs_data_message_conjugates_and_counts_zero_price():
    """One price=£0 comp: the verb agrees ('1 sale exists', not '1 sale exist')
    and the unusable zero price is attributed to 'no sold price' rather than
    degrading to the generic fallback clause."""
    subject = make_subject(sqft=None, lease=990)
    v = value_verdict(subject, [make_comp(None, price=0, tid="zero")])
    assert v.tag == ValueTag.NEEDS_DATA
    r = v.reasons[0]
    assert "1 sale exists" in r
    assert "1 carries no sold price" in r
    assert "sale exist " not in r                        # the old broken verb


# ---------------------------------------------------------------------------
# 8 · Honest wrong-class NEEDS_DATA on the £/sqft path (comps exist, none
#     qualify) — the message must not claim "no sales nearby" when sales exist.
# ---------------------------------------------------------------------------

def test_wrong_class_needs_data_message_is_honest():
    """A HOUSE subject (with sqft) over all-flat cached comps previously claimed
    'no comparable sales nearby' — false. Now: sales exist, none comparable."""
    subject = make_subject(sqft=900, ptype="terraced", lease=990)
    comps = _llf_comps(ptype="flat-maisonette", n=5)
    v = value_verdict(subject, comps)
    assert v.tag == ValueTag.NEEDS_DATA
    r = v.reasons[0]
    assert "5 sales exist" in r and "none are comparable" in r
    assert "wrong property class" in r and "terraced" in r
    assert "floor area" in r.lower() and "unlock" in r.lower()   # what unlocks it
    assert "no comparable sales nearby" not in r.lower()


def test_with_sqft_subject_over_area_less_comps_gets_llf_not_needs_data():
    """The §5.2b INVERSION, closed: a subject WITH a floor area whose comps all
    lack one used to dead-end in NEEDS_DATA while the identical subject minus
    its sqft got a like-for-like verdict — supplying MORE data yielded LESS
    verdict. Now both route to the same llf fallback; only the wording differs
    (the gap is on the comp side, so 'no floor area was given' would be false)."""
    subject = make_subject(sqft=900, ptype="terraced", lease=990)
    comps = [make_comp(None, conf="high", same_street=True, gap=0.5,
                       price=800_000, ptype="terraced", tid="na%d" % i)
             for i in range(3)]
    v = value_verdict(subject, comps)
    assert v.tag != ValueTag.NEEDS_DATA                        # inversion closed
    assert "like-for-like" in v.basis
    assert "no comparable sale carries one" in v.basis.lower()  # comp-side gap named
    assert "no floor area was given" not in v.basis            # ...not blamed on the user
    assert v.streetMedianPerSqft is None                       # honestly absent: no £/sqft
    assert v.reasons and "floor area" in v.reasons[0].lower()
    # The identical subject minus its sqft reads the same numbers — one
    # fallback, two honest labels.
    no_sqft = dict(subject)
    no_sqft["sqft"] = None
    fallback = value_verdict(no_sqft, comps)
    assert fallback.tag == v.tag and fallback.fairEstimate == v.fairEstimate
    assert fallback.deltaPct == v.deltaPct and fallback.confidence == v.confidence
    assert "no floor area was given" in fallback.basis
    assert validate(v) == []


def test_with_sqft_subject_and_unusable_comps_still_needs_data():
    """The inversion routing must not over-reach: with-sqft subject + comps that
    fail LLF qualification too (no sold price) is still the honest NEEDS_DATA,
    with the area gap named and no stale 're-run without the floor area' hint
    (the fallback is automatic now)."""
    subject = make_subject(sqft=900, ptype="terraced", lease=990)
    comps = [make_comp(None, conf="high", same_street=True, gap=0.5,
                       price=0, ptype="terraced", tid="np%d" % i)
             for i in range(3)]
    v = value_verdict(subject, comps)
    assert v.tag == ValueTag.NEEDS_DATA
    r = v.reasons[0]
    assert "3 sales exist" in r and "no floor area" in r
    assert "without the floor area" not in r                   # stale hint retired


def test_truly_empty_comps_message_unchanged():
    """With NO cached sales at all, the original 'nothing nearby' line remains
    the truthful one."""
    v = value_verdict(make_subject(sqft=900, lease=990), [])
    assert v.tag == ValueTag.NEEDS_DATA
    assert "no comparable sales nearby" in v.reasons[0].lower()


# ---------------------------------------------------------------------------
# 9 · PPD transaction-category filter — non-open-market rows never anchor a
#     fair estimate, and the exclusions stay visible (honest, not silent).
# ---------------------------------------------------------------------------

def test_additional_category_comp_excluded_from_ppsf_anchor():
    """A repossession priced 40% under the street must not drag the fair
    estimate down: it is excluded from the anchor, and the basis says so with
    a count rather than pretending it never existed."""
    subject = make_subject(sqft=1000, ask=1_000_000, lease=990)
    standard = [make_comp(1000, tid="s%d" % i, category="standard")
                for i in range(6)]
    repo = make_comp(600, tid="repo", category="additional")
    v = value_verdict(subject, standard + [repo])
    clean = value_verdict(subject, standard)
    assert v.fairEstimate == clean.fairEstimate          # the repo moved nothing
    assert "1 non-standard sale excluded" in v.basis
    assert "additional price paid" in v.basis
    # ...and the excluded row is not cited as anchor evidence.
    assert not any(e.compRef and e.compRef.id == "repo" for e in v.evidence)
    # a fully-captured standard set carries no unknown-category caveat.
    assert "category unknown" not in v.basis
    assert validate(v) == []


def test_additional_category_comp_excluded_from_llf_anchor():
    """The like-for-like path applies the same gate: a power-of-sale whole
    price is no more an open-market signal than its £/sqft would be."""
    subject = make_subject(sqft=None, ask=1_000_000, lease=990)
    standard = [make_comp(None, price=1_000_000, tid="ls%d" % i,
                          category="standard") for i in range(5)]
    repo = make_comp(None, price=500_000, tid="lrepo", category="additional")
    v = value_verdict(subject, standard + [repo])
    assert v.fairEstimate == 1_000_000                   # median of standard only
    assert "1 non-standard sale excluded" in v.basis
    assert validate(v) == []


def test_unknown_category_treated_standard_but_counted():
    """A comp set with NO category field (built before the capture) still
    anchors — most PPD rows are standard — but the assumption is visible:
    the basis counts the unknowns instead of silently claiming a vetted set."""
    subject = make_subject(sqft=1000, ask=1_000_000, lease=990)
    comps = [make_comp(1000, tid="u%d" % i) for i in range(6)]   # no category key
    v = value_verdict(subject, comps)
    assert v.tag != ValueTag.NEEDS_DATA                  # unknown != disqualified
    assert "transaction category unknown on 6 of 6 anchor comps" in v.basis
    assert "treated as standard" in v.basis


def test_all_additional_comps_yield_honest_needs_data():
    """When every sale on the street is non-standard, the NEEDS_DATA reason
    attributes them to the category filter — never 'no sales nearby', and
    never mislabelled as a class or data problem."""
    subject = make_subject(sqft=1000, ask=1_000_000, lease=990)
    comps = [make_comp(1000, tid="ar%d" % i, category="additional")
             for i in range(4)]
    v = value_verdict(subject, comps)
    assert v.tag == ValueTag.NEEDS_DATA
    r = v.reasons[0]
    assert "4 sales exist" in r
    assert "non-standard" in r and "additional price paid" in r
    assert "no comparable sales nearby" not in r.lower()


def test_hpi_adjust_keeps_transaction_category_on_dataclass_comps():
    """dataclasses.replace copies declared fields only, so the HPI time-adjust
    used to silently strip the category instance attribute from every moved
    Comp — overstating the unknown-category count. Pinned: the adjusted copy
    keeps the category and the original is not mutated."""
    from gaff_engine.value import _adjust_comp
    from gaff_engine.schemas import Comp
    c = Comp(price=800_000, date="2023-06-01", propertyType="flat-maisonette",
             pricePerSqft=1000.0)
    c.transactionCategory = "additional"
    moved = _adjust_comp(c, 0.9)
    assert moved is not c and moved.pricePerSqft == 900.0
    assert moved.transactionCategory == "additional"
    unmarked = Comp(price=800_000, date="2023-06-01",
                    propertyType="flat-maisonette", pricePerSqft=1000.0)
    assert getattr(_adjust_comp(unmarked, 0.9), "transactionCategory", None) is None


def test_full_label_category_is_normalised_not_waved_through():
    """A dict comp carrying the register's FULL label ("Additional price paid
    transaction") — not the pre-normalised word — must still be excluded and
    counted. A bare lowercase compare once let exactly this spelling slip both
    the anchor filter and the provenance count: != "additional" so it anchored,
    not None so it was never counted as unknown."""
    from gaff_engine.value import qualifies
    subject = make_subject(sqft=1000, ask=1_000_000, lease=990)
    repo = make_comp(600, tid="full-label",
                     category="Additional price paid transaction")
    assert qualifies(repo, subject) is False
    slug = make_comp(600, tid="slug",
                     category="additionalPricePaidTransaction")
    assert qualifies(slug, subject) is False
    ok = make_comp(1000, tid="full-std",
                   category="Standard price paid transaction")
    assert qualifies(ok, subject) is True
    comps = [make_comp(1000, tid="fs%d" % i, category="standard")
             for i in range(5)] + [repo]
    v = value_verdict(subject, comps)
    assert "1 non-standard sale excluded" in v.basis
    assert v.streetMedianPerSqft == 1000                 # the repo never anchored


def test_unrecognised_category_string_counts_as_unknown():
    """An unreadable category ("Something else entirely") is neither standard
    nor excluded — it must land in the unknown-category provenance count, the
    same honesty rule as a missing field, never a silent pass as standard."""
    subject = make_subject(sqft=1000, ask=1_000_000, lease=990)
    comps = [make_comp(1000, tid="w%d" % i, category="Something else entirely")
             for i in range(6)]
    v = value_verdict(subject, comps)
    assert v.tag != ValueTag.NEEDS_DATA                  # unknown != disqualified
    assert "transaction category unknown on 6 of 6 anchor comps" in v.basis


def test_loader_normalises_full_label_category():
    """_comp_from_dict mirrors the same normalisation, so an enriched file
    carrying the full label loads as the canonical vocabulary the qualifiers
    compare against (and an unreadable one loads as unknown, not a free pass)."""
    from gaff_engine.value import _comp_from_dict
    d = {"price": 800_000, "date": "2023-06-01", "pricePerSqft": 1000.0,
         "propertyType": "flat-maisonette",
         "transactionCategory": "Additional price paid transaction"}
    assert _comp_from_dict(d).transactionCategory == "additional"
    d["transactionCategory"] = "standardPricePaidTransaction"
    assert _comp_from_dict(d).transactionCategory == "standard"
    d["transactionCategory"] = "Something else entirely"
    assert _comp_from_dict(d).transactionCategory is None


def test_inversion_basis_honest_when_area_comps_are_category_excluded():
    """The §5.2b inversion wording must not lie about WHY the £/sqft anchor is
    empty: with-sqft subject + same-class 'additional' comps WITH a £/sqft +
    standard price-only comps routes to the llf fallback — but three
    comparable sales DO carry floor areas, so 'no comparable sale carries one'
    would be false. The basis blames the category exclusion instead."""
    subject = make_subject(sqft=1000, ask=1_000_000, lease=990)
    comps = ([make_comp(600, tid="ax%d" % i, category="additional")
              for i in range(3)]
             + [make_comp(None, price=1_000_000, tid="po%d" % i,
                          category="standard") for i in range(3)])
    v = value_verdict(subject, comps)
    assert v.tag != ValueTag.NEEDS_DATA                  # still a real llf verdict
    assert "like-for-like" in v.basis
    assert "no comparable sale carries one" not in v.basis.lower()
    assert "non-standard" in v.basis
    assert "excluded" in v.basis
    assert "open-market sale with a floor area" in v.reasons[-1]
    # The genuinely area-less inversion keeps its original truthful wording.
    bare = value_verdict(subject, [make_comp(None, price=1_000_000,
                                             tid="bp%d" % i, category="standard")
                                   for i in range(3)])
    assert "no comparable sale carries one" in bare.basis.lower()
    assert validate(v) == []


def test_real_enriched_cache_carries_categories_and_excludes_additional():
    """data/comps_enriched.json was regenerated 2026-08-29 WITH the PPD
    transaction category (enrich_run.comp_payload carries it explicitly,
    because serialize.to_jsonable emits declared dataclass fields only and
    would strip the instance attribute). Pinned here so a future persist that
    drops the field again fails loudly: every loaded comp reads a real
    category, the real verdict excludes the non-standard rows with a visible
    count, and the unknown-category caveat no longer fires."""
    comps = load_enriched_comps()
    cats = {getattr(c, "transactionCategory", None) for c in comps}
    assert None not in cats, "regenerated enriched file must carry a category on every comp"
    assert cats <= {"standard", "additional"}
    assert any(getattr(c, "transactionCategory", None) == "additional" for c in comps), \
        "the De Beauvoir matched set is known to contain additional-category rows"
    v = value_verdict(GOLDEN_LISTING, comps)
    assert "non-standard sales excluded" in v.basis or "non-standard sale excluded" in v.basis
    assert "transaction category unknown" not in v.basis


def test_raw_enriched_file_persists_transaction_category_key():
    """The persist seam itself, pinned on the artifact: every comp dict in
    data/comps_enriched.json carries the transactionCategory key (null =
    unknown is allowed by the shape; a MISSING key means the persist path
    regressed to bare to_jsonable and the exclusion went inert)."""
    import json as _json
    from gaff_engine.value import DEFAULT_COMPS_PATH
    with open(DEFAULT_COMPS_PATH) as f:
        blob = _json.load(f)
    raw = blob["comps"]
    assert raw and all("transactionCategory" in c for c in raw)


# ---------------------------------------------------------------------------
# 10 · Sqft basis flag — marketing sqft vs a supplied EPC-derived area. The
#      engine prices on the marketing figure (the property as currently
#      marketed); a conflict beyond tolerance dents confidence and is named.
# ---------------------------------------------------------------------------

def _basis_comps(n=6):
    return [make_comp(1000, tid="sb%d" % i, category="standard") for i in range(n)]


def test_sqft_basis_conflict_flagged_and_dents_confidence():
    subject = make_subject(sqft=1000, ask=1_000_000, lease=990)
    subject["epcSqft"] = 700                             # 43% above the EPC area
    v = value_verdict(subject, _basis_comps())
    plain = value_verdict(make_subject(sqft=1000, ask=1_000_000, lease=990),
                          _basis_comps())
    assert "sqft basis conflict" in v.basis
    assert "marketing 1,000 sqft vs EPC 700 sqft" in v.basis
    assert "priced on the marketing figure" in v.basis   # which figure, stated
    assert v.confidence < plain.confidence               # the conflict costs trust
    assert v.fairEstimate == plain.fairEstimate          # ...but never moves the number
    assert validate(v) == []


def test_sqft_within_tolerance_or_absent_no_flag():
    """Convention noise (a few % between marketing and EPC measurement) and a
    missing EPC area both stay silent — the flag never fires on a guess."""
    close = make_subject(sqft=1000, ask=1_000_000, lease=990)
    close["epcSqft"] = 950                               # 5.3% apart: normal daylight
    v_close = value_verdict(close, _basis_comps())
    assert "sqft basis conflict" not in v_close.basis
    v_absent = value_verdict(make_subject(sqft=1000, ask=1_000_000, lease=990),
                             _basis_comps())
    assert "sqft basis conflict" not in v_absent.basis
    assert v_close.confidence == v_absent.confidence


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest) — mirrors tests/test_u9_landreg.py.
# ---------------------------------------------------------------------------

def _run_standalone():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print("FAIL  %s\n      %s" % (name, e))
        except Exception as e:  # unexpected error
            failures += 1
            print("ERROR %s\n      %s: %s" % (name, type(e).__name__, e))
        else:
            print("PASS  %s" % name)
    print("-" * 60)
    total = len(tests)
    if failures:
        print("RESULT: FAIL (%d/%d passed, %d failed)" % (total - failures, total, failures))
    else:
        print("RESULT: PASS (%d/%d passed)" % (total, total))
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
