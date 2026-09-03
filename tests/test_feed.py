"""Feed tests — assembleFeed, the Buy shortlist (05-modes §5.3).

DETERMINISTIC: scores the golden + the three demo listings (real value verdicts
from the on-disk comps) and asserts the ranked shortlist. No network.

    python3 -m pytest tests/test_feed.py -v
    python3 tests/test_feed.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.engine import score  # noqa: E402
from gaff_engine.feed import assemble_feed  # noqa: E402
from gaff_engine.schemas import FeedLayout, Stage  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import (  # noqa: E402
    GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH,
)
from gaff_engine.fixtures.shortlist import demo_shortlist  # noqa: E402


def _scored():
    listings = [GOLDEN_LISTING]
    results = [score(GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH)]
    for d in demo_shortlist():
        listings.append(d["listing"])
        results.append(score(d["listing"], GOLDEN_PERSON, GOLDEN_SEARCH,
                             taste_model=d["taste_model"], forensics_model=d["forensics_model"]))
    return results, listings


def _feed():
    results, listings = _scored()
    return assemble_feed(results, listings, GOLDEN_PERSON, GOLDEN_SEARCH)


# ---------------------------------------------------------------------------
# 1 · The shortlist ranks by composite, descending.
# ---------------------------------------------------------------------------

def test_feed_ranks_by_composite_desc():
    feed = _feed()
    assert isinstance(feed, FeedLayout) and feed.stage == Stage.BROWSE
    comps = [c.composite for c in feed.cards]
    assert comps == sorted(comps, reverse=True)
    assert len(feed.cards) == 4


def test_each_card_is_a_calm_browse_pair():
    """Every card carries exactly the two browse-stage lead Slots (§5.3): the
    compact Value Verdict + risk_flags — never a heavy Component."""
    for c in _feed().cards:
        codes = [s.component.value for s in (c.slots or [])]
        assert codes == ["value_verdict", "risk_flags"]
        vv = [s for s in c.slots if s.component.value == "value_verdict"][0]
        assert vv.form == "compact"


# ---------------------------------------------------------------------------
# 2 · The truth layer DISCRIMINATES across the set — a steal, a fair, an over.
# ---------------------------------------------------------------------------

def test_verdict_spread_steal_fair_over():
    tags = {c.verdictTag.value for c in _feed().cards if c.verdictTag}
    assert {"steal", "fair", "over"} <= tags        # the differentiator, working across a shortlist


def test_demo_listings_flagged_golden_is_real():
    by = {(c.addressDisplay or "").split(",")[0]: c for c in _feed().cards}
    assert by["Northchurch Road"].isDemo is False   # the golden is the real anchor
    assert by["Englefield Road"].isDemo is True      # demos are marked honestly
    assert by["De Beauvoir Road"].isDemo is True


# ---------------------------------------------------------------------------
# 3 · Determinism + narration bounded to the set.
# ---------------------------------------------------------------------------

def test_feed_is_deterministic():
    def sig(f):
        return [(c.listingRef.id, c.composite, c.verdictTag.value if c.verdictTag else None)
                for c in f.cards]
    assert sig(_feed()) == sig(_feed())


def test_narration_states_the_spread():
    feed = _feed()
    head = feed.narration.headline
    assert "4 home" in head
    assert "steal" in head and "over" in head       # cites the real spread, invents nothing


def test_feed_is_schema_valid():
    assert validate(_feed()) == []


def test_excluded_listings_never_compete():
    """A hard-gate-excluded result (composite 0) is dropped from the shortlist."""
    import dataclasses
    from gaff_engine.schemas import Gate
    results, listings = _scored()
    # a distinct one-bath listing, hard-gate-excluded on min_baths >= 2.
    bad = dataclasses.replace(GOLDEN_LISTING, id="listing_excluded_1bath", baths=1)
    search = dataclasses.replace(GOLDEN_SEARCH, gates=[Gate(code="min_baths", op=">=", value=2)])
    excluded = score(bad, GOLDEN_PERSON, search)
    assert excluded.rules.excluded is True
    feed = assemble_feed([excluded] + results, [bad] + listings, GOLDEN_PERSON, GOLDEN_SEARCH)
    ids = [c.listingRef.id for c in feed.cards]
    assert "listing_excluded_1bath" not in ids           # dropped — never competes
    assert len(feed.cards) == 4                            # the four scored homes remain


# ---------------------------------------------------------------------------
# Plain-stdlib runner.
# ---------------------------------------------------------------------------

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
