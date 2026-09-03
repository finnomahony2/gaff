"""U-rent + the one-library-four-products gate (05-modes §5.3 / A1, A2).

The architecture payoff: the SAME engine + the SAME renderer produce a
felt-different Rent product from a rent mode.profile@1 — the asking-rent value
verdict (not Land Registry), the 60/25/15 Mix, the affordability lead, ownership
Components suppressed — and one score.result renders four different leads across
the four lenses.

    python3 -m pytest tests/test_rent.py -v
    python3 tests/test_rent.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.composite import composite as composite_mix  # noqa: E402
from gaff_engine.dashboard import (  # noqa: E402
    BUY_PROFILE, DREAM_PROFILE, INVEST_PROFILE, RENT_PROFILE, assemble_dashboard,
)
from gaff_engine.engine import score  # noqa: E402
from gaff_engine.forensics import canonical_model as canonical_forensics  # noqa: E402
from gaff_engine.rent import affordability, rent_listing, rent_verdict  # noqa: E402
from gaff_engine.taste import canonical_model  # noqa: E402
from gaff_engine.elicit import person_from_answers  # noqa: E402
from gaff_engine.schemas import (  # noqa: E402
    Budget, Money, Mode, Ref, ScorerMix, Search, Threshold,
)
from gaff_engine.validate import validate  # noqa: E402


def _person():
    return person_from_answers({"name": "Finn"})


def _raw(pcm, outcode="EC1V", beds=2, lid="subj"):
    return {"id": lid, "outcode": outcode, "beds": beds, "pcm": pcm, "type": "flat",
            "address": "%s, London %s" % (outcode, outcode), "sqft": 800,
            "key_features": ["Two bedrooms", "Furnished"], "description": "A flat."}


def _pool(median_pcm, n=8, outcode="EC1V", beds=2):
    return [{"id": "c%d" % i, "outcode": outcode, "beds": beds, "pcm": median_pcm} for i in range(n)]


def _rent_search(mix=(60, 25, 15), budget_max=6500):
    return Search(id="search_rent", mode=Mode.RENT,
                  scorerMix=ScorerMix(taste=mix[0], rules=mix[1], value=mix[2]),
                  threshold=Threshold(show=6.0, alert=7.5), gates=[],
                  budget=Budget(max=Money(amount=budget_max)),
                  personRef=Ref(id="person_finn", schemaVersion="person@1"))


# ---------------------------------------------------------------------------
# The asking-rent value verdict (§5.3): £pcm vs the area, NOT Land Registry.
# ---------------------------------------------------------------------------

def test_asking_rent_verdict_reads_the_spread():
    pool = _pool(4000)
    steal = rent_verdict(rent_listing(_raw(3400)), pool)     # 15% under the area median
    fair = rent_verdict(rent_listing(_raw(4000)), pool)      # right on it
    over = rent_verdict(rent_listing(_raw(4600)), pool)      # 15% over

    assert steal.tag == "steal" and steal.deltaPct == -15.0
    assert fair.tag == "fair" and fair.deltaPct == 0.0
    assert over.tag == "over" and over.deltaPct == 15.0
    # the fair estimate is the area median £pcm, and the basis names it a rent read
    assert fair.fairEstimate == 4000 and "Land Registry" in fair.basis
    assert validate(steal) == [], validate(steal)
    # a cheaper let scores higher on the value slot than a dearer one
    assert steal.score > fair.score > over.score


# ---------------------------------------------------------------------------
# The rent Scorer Mix — 60 / 25 / 15 (§5.3), applied by the engine.
# ---------------------------------------------------------------------------

def test_rent_mix_is_60_25_15():
    pool = _pool(4200)
    L = rent_listing(_raw(4200))
    person = _person()
    r = score(L, person, _rent_search(), taste_model=canonical_model(),
              forensics_model=canonical_forensics(), comps=pool)
    assert validate(r) == [], validate(r)[:3]
    # the composite is genuinely the rent Mix over the three live scorers
    expected = composite_mix(r.taste.score, r.rules.score, r.valueVerdict.score,
                             ScorerMix(taste=60, rules=25, value=15))
    assert r.composite == expected
    # and it differs from the Buy Mix over the same scores (the lens changed the weight)
    buy_composite = composite_mix(r.taste.score, r.rules.score, r.valueVerdict.score,
                                  ScorerMix(taste=55, rules=20, value=25))
    assert r.composite != buy_composite
    # the value slot read asking rent, not Land Registry (honest provenance)
    assert any("asking-rent" in d for d in r.confidence.drivers)


# ---------------------------------------------------------------------------
# The Rent dashboard — leads with affordability + commute, ownership suppressed.
# ---------------------------------------------------------------------------

def test_rent_dashboard_leads_affordability_suppresses_ownership():
    pool = _pool(4200)
    L = rent_listing(_raw(3900))
    person = _person()
    search = _rent_search()
    r = score(L, person, search, taste_model=canonical_model(),
              forensics_model=canonical_forensics(), comps=pool)
    dash = assemble_dashboard(r, person, search, L, mode_profile=RENT_PROFILE, pursuit="shortlist")
    comps = [_c(s) for s in dash.slots]
    present = set(comps)

    assert comps[0] == "affordability"                      # the lead
    assert "commute_isochrone" in present                   # co-leads
    assert "taste_breakdown" in present                     # taste still leads the Mix
    # ownership Components are suppressed for rent (no lease, no negotiation)
    assert not ({"cost_of_ownership", "negotiation", "comps_table", "price_history"} & present)
    assert validate(dash) == [], validate(dash)[:3]


# ---------------------------------------------------------------------------
# A1 — one renderer, four products. One score.result, four profiles, four leads.
# ---------------------------------------------------------------------------

def test_one_library_four_products():
    pool = _pool(4200)
    L = rent_listing(_raw(4000))
    person = _person()
    r = score(L, person, _rent_search(), taste_model=canonical_model(),
              forensics_model=canonical_forensics(), comps=pool)

    # the four profiles LEAD with four distinct Components (no per-mode renderer)
    leads = [BUY_PROFILE.lead[0], RENT_PROFILE.lead[0], INVEST_PROFILE.lead[0], DREAM_PROFILE.lead[0]]
    assert leads == ["value_verdict", "affordability", "deal_table", "imagery"]
    assert len(set(leads)) == 4

    # the same score.result rendered through each per-listing lens → a different slot[0]
    def slot0(mode, prof):
        s = Search(id="s_" + mode, mode=Mode(mode), scorerMix=ScorerMix(taste=60, rules=25, value=15),
                   threshold=Threshold(show=6.0, alert=7.5), gates=[],
                   budget=Budget(max=Money(amount=6500)), personRef=r.request.personRef)
        d = assemble_dashboard(r, person, s, L, mode_profile=prof, pursuit="shortlist")
        return _c(d.slots[0]) if d.slots else None

    assert slot0("buy", BUY_PROFILE) == "value_verdict"
    assert slot0("rent", RENT_PROFILE) == "affordability"
    assert slot0("dream", DREAM_PROFILE) == "imagery"
    # invest leads with the deal_table, a cross-listing FEED surface (routed out of the
    # per-listing dashboard, §5.4) — proven at the profile level, above.


def test_golden_buy_dashboard_unchanged():
    """A2 — the Buy lens still leads with the truth; M7 didn't disturb it."""
    from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH
    from gaff_engine.forensics import canonical_model as fc
    r = score(GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH,
              taste_model=canonical_model(), forensics_model=fc())
    dash = assemble_dashboard(r, GOLDEN_PERSON, GOLDEN_SEARCH, GOLDEN_LISTING, pursuit="browse")
    assert _c(dash.slots[0]) == "value_verdict" and _c(dash.slots[1]) == "risk_flags"
    assert r.composite == 7.6                               # the buy value path is untouched (HPI-adjusted golden)


# ---------------------------------------------------------------------------
# Affordability — the rent lead's read (§5.3).
# ---------------------------------------------------------------------------

def test_affordability_bands():
    within = affordability(rent_listing(_raw(4500)), budget_max_pcm=6500, budget_gravity_pcm=5000)
    stretch = affordability(rent_listing(_raw(6000)), budget_max_pcm=6500, budget_gravity_pcm=5000)
    over = affordability(rent_listing(_raw(7200)), budget_max_pcm=6500, budget_gravity_pcm=5000)
    assert within["tag"] == "within" and within["headroomPct"] > 0
    assert stretch["tag"] == "stretch"
    assert over["tag"] == "over" and "over your ceiling" in over["line"]


def _c(slot):
    return slot.component.value if hasattr(slot.component, "value") else slot.component


def _run_standalone():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print("FAIL  %s\n      %s" % (name, e))
        except Exception as e:
            failures += 1
            print("ERROR %s\n      %s: %s" % (name, type(e).__name__, e))
        else:
            print("PASS  %s" % name)
    print("-" * 60)
    total = len(tests)
    print("RESULT: %s (%d/%d passed%s)" % (
        "FAIL" if failures else "PASS", total - failures, total,
        ", %d failed" % failures if failures else ""))
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
