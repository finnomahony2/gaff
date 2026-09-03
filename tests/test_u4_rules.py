"""U4 tests — the Rules scorer + gates engine (03-engine §5.3).

DETERMINISTIC: never hits the network. Drives the real GOLDEN_SEARCH /
GOLDEN_LISTING fixtures plus small synthetic listings/searches built from light
dicts (the accessors read Listing / Search / Gate / dict alike, the codebase
style). Reproducible on every run.

Runnable two ways (matching tests/test_u3_value.py):

    python3 -m pytest tests/test_u4_rules.py -v     # if pytest is installed
    python3 tests/test_u4_rules.py                  # plain-stdlib fallback
"""

import os
import sys

# Make the repo root importable whether run by pytest (from root) or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.rules import (  # noqa: E402
    CONFIG, apply_gates, evaluate_gates, rules_confidence, rules_result,
    rules_score,
)
from gaff_engine.schemas import (  # noqa: E402
    Flag, FlagCode, Gate, GateResult, Reason, RulesResult, SoftDock,
)
from gaff_engine.validate import validate  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_SEARCH  # noqa: E402


# ---------------------------------------------------------------------------
# Light synthetic builders (Gate objects inside dict Searches; dict Listings).
# ---------------------------------------------------------------------------

def make_search(*gates, polygon=None):
    s = {"gates": list(gates)}
    if polygon is not None:
        s["area"] = {"polygon": polygon}
    return s


def make_listing(beds=2, baths=2, sqft=1050, price=1_150_000, lease=89,
                 tenure="leasehold", outdoor=True, geo=None, receptions=2):
    kf = ["Two double bedrooms, two bathrooms"]
    if outdoor:
        kf.append("Private south-west garden")
    listing = {
        "beds": beds, "baths": baths, "sqft": sqft, "receptions": receptions,
        "keyFeatures": kf,
        "description": "A characterful period maisonette."
                       + (" With a private garden." if outdoor else " No outside space."),
        "buy": {"price": {"amount": price},
                "tenure": {"type": tenure, "leaseYearsRemaining": lease}},
    }
    if geo is not None:
        listing["geo"] = geo
    return listing


# The four standard hard gates + the soft lease gate, mirroring GOLDEN_SEARCH.
def std_gates():
    return [
        Gate(code="min_beds", op=">=", value=2),
        Gate(code="min_baths", op=">=", value=2),
        Gate(code="min_sqft", op=">=", value=900, unit="sqft"),
        Gate(code="lease_years_min", op=">=", value=90, unit="years", soft=True),
    ]


# ---------------------------------------------------------------------------
# 1 · PASS-ALL — the golden subject against the golden Buy search.
# ---------------------------------------------------------------------------

def test_golden_subject_passes_all_hard_gates():
    """The De Beauvoir subject is NOT excluded and scores 7.5 (§5.3 worked
    example: 8.0 base + 0.0 modest margins − 0.5 soft lease dock)."""
    excluded, reasons = apply_gates(GOLDEN_LISTING, GOLDEN_SEARCH)
    assert excluded is False and reasons == []
    rr = rules_result(GOLDEN_LISTING, GOLDEN_SEARCH)
    assert rr.excluded is False
    assert rr.gatesPassed is True
    assert rr.score == 7.5                       # the stated golden rules score
    assert rules_score(GOLDEN_LISTING, GOLDEN_SEARCH) == 7.5
    # the sub-90 lease is a SOFT dock, not an exclusion.
    assert rr.softDocks == [SoftDock(rule="lease_years_min (soft)", delta=-0.5)]
    # rules confidence is high by construction (all gates verified) — matches
    # the golden ConfidenceReport.rules = 0.85 (§5.8).
    assert rr.confidence == 0.85


def test_golden_gate_results_shape():
    """Every declared gate is evaluated; the soft lease row is passed=False,
    soft=True while the hard gates read soft=None (matches the golden shape)."""
    rr = rules_result(GOLDEN_LISTING, GOLDEN_SEARCH)
    by_code = {g.code: g for g in rr.gateResults}
    assert by_code["min_beds"].passed is True and by_code["min_beds"].soft is None
    assert by_code["min_baths"].passed is True
    assert by_code["min_sqft"].passed is True
    assert by_code["outdoor_present"].passed is True
    assert by_code["inside_polygon"].passed is True
    assert by_code["lease_years_min"].passed is False
    assert by_code["lease_years_min"].soft is True


# ---------------------------------------------------------------------------
# 2 · HARD-GATE EXCLUSION — bathroom, price-over-max, beds-below-min (A7).
# ---------------------------------------------------------------------------

def test_hard_gate_bathroom_excludes():
    """1 bathroom against a min_baths(2) gate -> excluded, score 0, and the
    reason names the bathroom gate."""
    search = make_search(Gate(code="min_baths", op=">=", value=2))
    listing = make_listing(baths=1)
    excluded, reasons = apply_gates(listing, search)
    assert excluded is True
    assert any("bath" in r for r in reasons), reasons
    rr = rules_result(listing, search)
    assert rr.excluded is True and rr.gatesPassed is False
    assert rr.score == 0.0
    assert rules_score(listing, search) == 0.0
    assert any(g.code == "min_baths" and g.passed is False for g in rr.gateResults)
    assert any("bath" in r.text for r in rr.reasons), rr.reasons


def test_hard_gate_price_above_max_excludes():
    """A price above a max_price gate -> excluded, reason names the price gate."""
    search = make_search(Gate(code="max_price", op="<=", value=1_350_000))
    listing = make_listing(price=1_500_000)
    excluded, reasons = apply_gates(listing, search)
    assert excluded is True
    assert any("price" in r for r in reasons), reasons
    rr = rules_result(listing, search)
    assert rr.excluded is True and rr.score == 0.0
    # a price at/under the max is a clean pass (not excluded).
    ok = rules_result(make_listing(price=1_200_000), search)
    assert ok.excluded is False


def test_hard_gate_beds_below_min_excludes():
    """1 bed against a min_beds(2) gate -> excluded, reason names the beds gate."""
    search = make_search(Gate(code="min_beds", op=">=", value=2))
    listing = make_listing(beds=1)
    excluded, reasons = apply_gates(listing, search)
    assert excluded is True
    assert any("min_beds" in r or "bed" in r for r in reasons), reasons
    assert rules_result(listing, search).score == 0.0


# ---------------------------------------------------------------------------
# 3 · LEASE GATE — soft flags & docks (not excludes); hard excludes; long
#     lease passes clean; freehold is a clean pass (§5.3, A7).
# ---------------------------------------------------------------------------

def test_lease_soft_gate_flags_and_docks_not_excludes():
    """A sub-floor lease on a SOFT gate flags + docks −0.5 but does NOT exclude."""
    search = make_search(Gate(code="lease_years_min", op=">=", value=90, soft=True))
    rr = rules_result(make_listing(lease=89), search)
    assert rr.excluded is False and rr.gatesPassed is True
    assert rr.softDocks == [SoftDock(rule="lease_years_min (soft)", delta=-0.5)]
    assert rr.score == 7.5                                   # 8.0 − 0.5
    assert any(f.code == FlagCode.SHORT_LEASE for f in rr.flags)


def test_lease_long_passes_clean():
    """A comfortably long lease clears the gate: no dock, no short-lease flag."""
    search = make_search(Gate(code="lease_years_min", op=">=", value=90, soft=True))
    rr = rules_result(make_listing(lease=150), search)
    assert rr.softDocks is None
    assert rr.score == 8.0
    assert not any(f.code == FlagCode.SHORT_LEASE for f in rr.flags)


def test_lease_hard_gate_excludes():
    """The same sub-floor lease on a HARD gate (soft omitted) -> excluded."""
    search = make_search(Gate(code="lease_years_min", op=">=", value=90))  # hard
    rr = rules_result(make_listing(lease=89), search)
    assert rr.excluded is True and rr.score == 0.0


def test_freehold_is_clean_lease_pass():
    """A freehold listing has no lease term -> the lease gate passes clean,
    never docks or flags (no null-field noise)."""
    search = make_search(Gate(code="lease_years_min", op=">=", value=90, soft=True))
    rr = rules_result(make_listing(tenure="freehold", lease=None), search)
    assert rr.excluded is False and rr.softDocks is None
    assert not any(f.code == FlagCode.SHORT_LEASE for f in rr.flags)


# ---------------------------------------------------------------------------
# 4 · OUTDOOR GATE — required-outdoor search excludes a no-outdoor listing.
# ---------------------------------------------------------------------------

def test_outdoor_gate_excludes_when_absent():
    """outdoor_present(==True) is a hard gate: a listing with no outdoor space
    is excluded; one with a garden passes (§5.3, A7)."""
    search = make_search(Gate(code="outdoor_present", op="==", value=True))
    no_outdoor = rules_result(make_listing(outdoor=False), search)
    assert no_outdoor.excluded is True and no_outdoor.score == 0.0
    assert any(g.code == "outdoor_present" and g.passed is False
               for g in no_outdoor.gateResults)
    has_outdoor = rules_result(make_listing(outdoor=True), search)
    assert has_outdoor.excluded is False


# ---------------------------------------------------------------------------
# 5 · SCORE MONOTONICITY — comfortably exceeding scores >= just meeting.
# ---------------------------------------------------------------------------

def test_score_monotonic_margin_bonus():
    """A listing that comfortably exceeds the size/count gates scores strictly
    higher than one that only just meets them (margin bonus, §5.3)."""
    search = make_search(
        Gate(code="min_beds", op=">=", value=2),
        Gate(code="min_baths", op=">=", value=2),
        Gate(code="min_sqft", op=">=", value=900, unit="sqft"),
    )
    just_meets = rules_result(make_listing(beds=2, baths=2, sqft=900), search)
    exceeds = rules_result(make_listing(beds=4, baths=3, sqft=1500), search)
    assert just_meets.score == 8.0                          # no margin, no docks
    assert exceeds.score >= just_meets.score
    assert exceeds.score > just_meets.score                 # margin bonus lifts it


def test_score_monotonic_soft_dock():
    """Independent of margins: clearing the soft lease (no dock) scores >= failing
    it (−0.5 dock), all else equal."""
    search = make_search(*std_gates())
    with_dock = rules_result(make_listing(lease=89), search)     # soft fail
    no_dock = rules_result(make_listing(lease=150), search)      # soft pass
    assert no_dock.score >= with_dock.score
    assert no_dock.score - with_dock.score == 0.5


# ---------------------------------------------------------------------------
# 6 · SCHEMA-VALID — rules_result validates clean (pass-all and excluded).
# ---------------------------------------------------------------------------

def test_rules_result_is_schema_valid():
    """rules_result(...) is a contract-clean RulesResult, and its attached flags
    carry only valid FlagCode members / are individually schema-valid (§5.7)."""
    rr = rules_result(GOLDEN_LISTING, GOLDEN_SEARCH)
    assert isinstance(rr, RulesResult)
    assert validate(rr) == []                                # the U1 validator is happy
    assert all(isinstance(g, GateResult) for g in rr.gateResults)
    assert all(validate(g) == [] for g in rr.gateResults)
    assert all(isinstance(f, Flag) and validate(f) == [] for f in rr.flags)
    assert all(isinstance(r, Reason) and validate(r) == [] for r in rr.reasons)


def test_excluded_result_is_schema_valid():
    """An excluded RulesResult (score 0, gatesPassed False) also validates."""
    search = make_search(Gate(code="min_baths", op=">=", value=2))
    rr = rules_result(make_listing(baths=1), search)
    assert rr.excluded is True and rr.score == 0.0
    assert validate(rr) == []


# ---------------------------------------------------------------------------
# 7 · ROBUSTNESS — null field lowers confidence, never excludes; unknown gate
#     kind is not enforced (declarative-table extensibility).
# ---------------------------------------------------------------------------

def test_null_sqft_does_not_exclude_lowers_confidence():
    """A min_sqft gate against a listing with no stated sqft cannot be verified:
    it does NOT exclude (never exclude on missing data, §7.4) and lowers the
    rules confidence below its 0.85 base (§5.8)."""
    search = make_search(Gate(code="min_sqft", op=">=", value=900))
    listing = make_listing(sqft=None)
    excluded, _ = apply_gates(listing, search)
    assert excluded is False
    rr = rules_result(listing, search)
    assert rr.excluded is False
    assert rr.confidence < 0.85
    assert any("sqft" in r.text for r in rr.reasons), rr.reasons


def test_unknown_gate_kind_not_enforced():
    """A gate whose code has no evaluator is not enforced (won't exclude) — new
    kinds plug in via the RESOLVERS table without breaking the engine."""
    search = make_search(Gate(code="min_garages", op=">=", value=1))
    rr = rules_result(make_listing(), search)
    assert rr.excluded is False
    # unenforceable -> counts as an unverified read -> confidence dips.
    assert rr.confidence < 0.85


def test_tenure_in_gate_excludes_disallowed_tenure():
    """A concrete tenure outside the allowed set is a genuine hard fail; an
    unknown tenure cannot be judged (unverified, not excluded)."""
    search = make_search(
        Gate(code="tenure_in", op="in", value=["freehold", "share_of_freehold"]))
    bad = rules_result(make_listing(tenure="leasehold"), search)
    assert bad.excluded is True                              # leasehold not allowed
    unknown = rules_result(make_listing(tenure="unknown", lease=None), search)
    assert unknown.excluded is False                         # can't judge -> don't exclude


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest) — mirrors tests/test_u3_value.py.
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
