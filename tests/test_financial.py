"""U-financial tests — the invest Financial scorer (03-engine.md §5.4).

The fourth product gets its own value slot: yield, not a purchase-price verdict.
- **The arithmetic reproduces the spec.** The worked Wales deal (£110,000 / £1,300 pcm)
  gives gross **14.2%** — matching its advertised figure — and a real net + cashflow; the
  computed gross matches the advertised `gross_yield_pct` across the whole pool.
- **The verdict is the financial analogue of steal/over.** A yield well ABOVE the local
  median is a `steal`; well below is `over` (this inverts the buy/rent direction).
- **The engine dispatches on mode.** `score(mode=invest)` fills the value slot from the
  Financial scorer; the golden Buy path stays byte-identical (composite 7.7).

    python3 -m pytest tests/test_financial.py -v
    python3 tests/test_financial.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.engine import score  # noqa: E402
from gaff_engine.elicit import person_from_profile  # noqa: E402
from gaff_engine.taste import canonical_model  # noqa: E402
from gaff_engine.forensics import canonical_model as canonical_forensics  # noqa: E402
from gaff_engine import paths  # noqa: E402
from gaff_engine.financial import (  # noqa: E402
    financial_verdict, financials, invest_listing, load_invest_pool, yield_cohort,
)
from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_SEARCH  # noqa: E402
from gaff_engine.schemas import (  # noqa: E402
    InvestDetails, Listing, Mode, Money, MoneyPeriod, Ref, ScorerMix, Search, Threshold,
)
from gaff_engine.validate import validate  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PERSON = person_from_profile(json.load(open(os.path.join(HERE, "profile.json"))))
# Pinned to the synthetic demo pool rather than load_invest_pool(), so the
# scorer is tested against a known fixture and the numbers below mean the
# same thing in a lab checkout and in the public package.
_POOL = json.load(open(paths.data_file("invest_pool.json")))
_WORKED = next(r for r in _POOL if r["id"] == 90000001)     # £110k / £1,300 pcm


def _invest_search():
    return Search(id="s_inv", mode=Mode.INVEST, scorerMix=ScorerMix(taste=30, rules=15, value=55),
                  threshold=Threshold(show=6.0, alert=7.5), gates=[],
                  personRef=Ref(id="person_a", schemaVersion="person@1"), title="South Wales BTL")


def _deal(price, rent_pcm, outcode="SA5"):
    return Listing(id="listing_test", listingKey="wales_test", mode=Mode.INVEST,
                   invest=InvestDetails(estRentPcm=Money(amount=rent_pcm, period=MoneyPeriod.PCM),
                                        price=Money(amount=price)),
                   address=__import__("gaff_engine.schemas", fromlist=["Address"]).Address(outcode=outcode))


# ---------------------------------------------------------------------------
# §5.4 — the yield arithmetic reproduces the spec.
# ---------------------------------------------------------------------------

def test_worked_example_gross_yield():
    fin = financials(invest_listing(_WORKED))
    assert fin.grossYieldPct == 14.2                    # 15,600 / 110,000 × 100 (matches advertised)
    assert fin.grossYieldPct == _WORKED["gross_yield_pct"]
    assert fin.annualRent == 15600
    # net after 3-wk voids + 10% mgmt + 5% maintenance, and the monthly cashflow
    assert fin.annualCosts == 3240 and fin.netYieldPct == 11.2
    assert fin.monthlyCashflow == 1030


def test_computed_gross_matches_advertised_across_the_pool():
    for raw in _POOL:
        fin = financials(invest_listing(raw))
        assert abs(fin.grossYieldPct - raw["gross_yield_pct"]) <= 0.15, raw["id"]


# ---------------------------------------------------------------------------
# §5.4 — the verdict is the financial analogue (high yield → steal).
# ---------------------------------------------------------------------------

def test_high_yield_is_a_steal_low_yield_is_over():
    strong = financial_verdict(_deal(110000, 1300), _POOL)   # 14.2% — well above the SA5 median
    assert strong.tag == "steal" and strong.deltaPct > 12 and strong.score > 6
    assert "gross 14.2%" in strong.basis and "median" in strong.basis
    weak = financial_verdict(_deal(300000, 1000), _POOL)     # 4.0% — far below any local median
    assert weak.tag == "over" and weak.deltaPct < -12 and weak.score < 4
    assert validate(strong) == [] and validate(weak) == []


def test_verdict_carries_the_financials_in_the_basis_and_reasons():
    vv = financial_verdict(invest_listing(_WORKED), _POOL)
    assert "net" in vv.basis and "cashflow" in vv.basis and "financing not modelled" in vv.basis
    assert any("gross yield" in r for r in (vv.reasons or []))
    assert vv.fairEstimate > 0                            # the price that would hit the local median yield


# ---------------------------------------------------------------------------
# §5.6 — the engine dispatches invest to the Financial scorer.
# ---------------------------------------------------------------------------

def test_engine_dispatches_invest_to_the_financial_scorer():
    L = invest_listing(_WORKED)
    r = score(L, _PERSON, _invest_search(), taste_model=canonical_model(),
              forensics_model=canonical_forensics(), comps=_POOL)
    assert validate(r) == [], validate(r)
    # the value slot IS the financial score, and the driver names the yield basis
    assert r.valueVerdict.score == financial_verdict(L, _POOL).score
    assert any("yield vs local median" in d for d in (r.confidence.drivers or []))


def test_golden_buy_path_is_unchanged():
    r = score(GOLDEN_LISTING, _PERSON, GOLDEN_SEARCH,
              taste_model=canonical_model(), forensics_model=canonical_forensics())
    assert r.composite == 7.6                             # invest wiring did not touch the Buy path (HPI-adjusted golden)
    assert "HM Land Registry" in " ".join(r.confidence.drivers or [])


def test_invest_listing_normalises_the_deal():
    L = invest_listing(_WORKED)
    assert L.mode == Mode.INVEST and L.invest.price.amount == 110000
    assert L.invest.estRentPcm.amount == 1300 and L.invest.grossYieldAdvertised == 14.2
    assert len(yield_cohort(L, _POOL)) >= 3               # SA5 has a real cohort


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
