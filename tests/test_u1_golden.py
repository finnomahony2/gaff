"""U1 golden tests — the De Beauvoir fixture is the quality oracle.

Runnable two ways:

    python3 -m pytest tests/test_u1_golden.py -v     # if pytest is installed
    python3 tests/test_u1_golden.py                  # plain-stdlib fallback

The plain-run path (a __main__ block) executes every ``test_*`` function with
the same asserts and prints PASS/FAIL, so U1 verifies with zero third-party deps.
"""

import os
import sys

# Make the repo root importable whether run by pytest (from root) or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.composite import composite, taste_score  # noqa: E402
from gaff_engine.schemas import ScoreResult  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import (  # noqa: E402
    GOLDEN_COMPONENT_SPEC, GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SCORE_RESULT,
    GOLDEN_SEARCH,
)

# The Buy Scorer Mix (55/20/25), read off the golden Search so the composite
# recompute is tied to the same config the result was scored against.
GOLDEN_MIX = GOLDEN_SEARCH.scorerMix


def test_a_golden_score_result_validates():
    """(a) validate(golden_fixture) returns no violations."""
    violations = validate(GOLDEN_SCORE_RESULT)
    assert violations == [], "golden ScoreResult should be contract-valid, got: %s" % violations


def test_a_sibling_fixtures_validate():
    """The Person / Search / Listing / ComponentSpec worked examples also validate."""
    for name, obj in [("Person", GOLDEN_PERSON), ("Search", GOLDEN_SEARCH),
                      ("Listing", GOLDEN_LISTING), ("ComponentSpec", GOLDEN_COMPONENT_SPEC)]:
        violations = validate(obj)
        assert violations == [], "golden %s should be contract-valid, got: %s" % (name, violations)


def test_b_taste_score_reproduces_golden():
    """(b) taste_score(golden axes, +0.30) == 8.2.

    base = Sigma(score*weight)/Sigma(weight) = 426.5/54.0 = 7.90;
    clamp(7.90 + 0.30) rounded to 1 dp = 8.2.
    """
    axes = GOLDEN_SCORE_RESULT.taste.axisBreakdown
    assert taste_score(axes, 0.30) == 8.2
    # The named-love adjustment can also be passed as the fixture's adjustment rows.
    assert taste_score(axes, GOLDEN_SCORE_RESULT.taste.tasteAdjustments) == 8.2


def test_b_weighted_base_is_426_5_over_54():
    """The base spine: Sigma weights = 54.0, Sigma(score*weight) = 426.5 (=> 7.90)."""
    axes = GOLDEN_SCORE_RESULT.taste.axisBreakdown
    total_weight = sum(a.weight for a in axes)
    weighted_sum = sum(a.score * a.weight for a in axes)
    assert total_weight == 54.0, "Sigma weights should be 54.0, got %s" % total_weight
    assert weighted_sum == 426.5, "Sigma(score*weight) should be 426.5, got %s" % weighted_sum
    # With no adjustment the base rounds to 7.9; the +0.30 named-love lifts it to 8.2.
    assert taste_score(axes, 0.0) == 7.9


def test_c_composite_reproduces_golden():
    """(c) composite(8.2, 7.5, 7.2, mix=55/20/25) == 7.8.

    (8.2*55 + 7.5*20 + 7.2*25)/100 = 781/100 = 7.81 -> 7.8.
    """
    assert composite(8.2, 7.5, 7.2, (55, 20, 25)) == 7.8
    # Same answer whether the mix is a triple, a dict, or the ScorerMix dataclass.
    assert composite(8.2, 7.5, 7.2, {"taste": 55, "rules": 20, "value": 25}) == 7.8
    assert composite(8.2, 7.5, 7.2, GOLDEN_MIX) == 7.8


def test_d_fixture_is_self_consistent():
    """(d) round-trip: the fixture's stored taste.score and composite equal the
    values recomputed from its own axisBreakdown / component scores + the Mix."""
    r = GOLDEN_SCORE_RESULT

    recomputed_taste = taste_score(r.taste.axisBreakdown, r.taste.tasteAdjustments)
    assert recomputed_taste == r.taste.score == 8.2, \
        "stored taste.score %s != recomputed %s" % (r.taste.score, recomputed_taste)

    recomputed_composite = composite(
        r.taste.score, r.rules.score, r.valueVerdict.score, GOLDEN_MIX)
    assert recomputed_composite == r.composite == 7.8, \
        "stored composite %s != recomputed %s" % (r.composite, recomputed_composite)

    # The verdict the arithmetic protects: headline steal (-11.7) reads honest fair (-8.2).
    assert r.valueVerdict.tag == "fair"
    assert r.valueVerdict.headlineDeltaPct == -11.7 and r.valueVerdict.deltaPct == -8.2
    assert r.rules.excluded is False and r.rules.gatesPassed is True


def test_validator_catches_a_broken_result():
    """Negative control: the validator must flag a contract breach, so a green
    (a) means something. A missing required field and a bad enum both surface."""
    import copy
    broken = copy.deepcopy(GOLDEN_SCORE_RESULT)
    broken.composite = None            # drop a required field
    broken.valueVerdict.tag = "bargain"  # not a ValueTag member
    violations = validate(broken)
    assert any("composite" in v for v in violations), violations
    assert any("tag" in v and "bargain" in v for v in violations), violations
    # And a non-dataclass is rejected outright.
    assert validate({"not": "a dataclass"}) != []


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest).
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
