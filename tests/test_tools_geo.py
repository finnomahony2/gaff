"""Tool-layer geographic routing — the comp pool follows the subject's town.

The L2C P0 this pins: ``comps_for_listing`` defaults to LONDON + the
De Beauvoir nearby set, so before ``_value_core`` routed by town, a
Leamington Spa paste was pooled against London sales and score_listing
confidently tagged it "steal" at -74% against a £1.75m "fair estimate" —
while price_check on the same street read the shipped Leamington cache
correctly. These tests drive the seam the release story promises (the
flagship one-call path reaching the second warm city) and the refusal
behaviour when no cache verifiably reaches the subject.

DETERMINISTIC + OFFLINE: everything reads the shipped warm caches
(london + leamington-spa comps, warwick + london HPI). No network.

    python3 -m pytest tests/test_tools_geo.py -v
    python3 tests/test_tools_geo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import tools  # noqa: E402


def _value(args):
    ok, payload = tools.safe_call("value_check", tools.value_check, args)
    assert ok, payload
    return payload


# ---------------------------------------------------------------------------
# 1 · The flagship one-call path reaches the second warm city (BACKLOG §2,
#     brief §5 item 1): a Leamington paste anchors on ITS OWN street.
# ---------------------------------------------------------------------------

def test_leamington_score_listing_anchors_same_street():
    ok, payload = tools.safe_call("score_listing", tools.score_listing, {
        "text": "3 bed terraced house for sale in Willes Road, "
                "Leamington Spa CV31 1BW, £450,000"})
    assert ok, payload
    value = payload["value"]
    assert value.get("error") is None
    assert value["tag"] is not None
    # The anchor is the subject's own street, adjusted in the subject's own
    # district's money — never the London pool, never London HPI.
    assert "same-street sold prices" in value["basis"]
    assert "(UK HPI, warwick)" in value["basis"]
    assert "london" not in value["basis"].lower()
    # The bug's shape: fairEstimate £1,750,000 off 97 London sales. A Willes
    # Road house anchor sits far below that.
    assert value["fairEstimate"] < 1_200_000
    assert payload["workings"]["addressMatch"]["matchLevel"] == "street"


def test_terse_leamington_paste_never_prices_against_london():
    """The exact paste the L2C review proved end-to-end. Its street is not
    extractable (no headline phrase, no line1), so the anchor honestly widens
    to Leamington's own cached streets — but never crosses to London."""
    payload = _value({"text": "terraced house, Willes Road, "
                              "Leamington Spa CV31 1BW, £450,000"})
    assert payload.get("error") is None
    assert payload["tag"] is not None
    assert "area sold prices" in payload["basis"]
    assert "(UK HPI, warwick)" in payload["basis"]
    assert "london" not in payload["basis"].lower()
    assert payload["fairEstimate"] < 1_200_000


# ---------------------------------------------------------------------------
# 2 · London routing is UNCHANGED — the enriched file + De Beauvoir set still
#     serve a subject that resolves to London (by name, outcode, or a street
#     cached under exactly one town).
# ---------------------------------------------------------------------------

def test_london_subject_keeps_the_london_pool():
    payload = _value({"fields": {
        "address": "De Beauvoir Road, London N1", "beds": 2, "baths": 2,
        "sqft": 1050, "price": 1150000, "property_type": "maisonette",
        "mode": "buy"}})
    assert payload.get("error") is None
    assert payload["tag"] in ("steal", "fair", "over")
    assert payload["comps_used"] > 50            # enriched + street caches


def test_bare_cached_street_resolves_by_uniqueness():
    """No town word, no outcode: a street cached under exactly ONE warmed
    town still routes (the surface suites' own "De Beauvoir Road" shape)."""
    payload = _value({"fields": {
        "address": "Willes Road", "beds": 3, "price": 450000,
        "property_type": "terraced", "mode": "buy"}})
    assert payload.get("error") is None
    assert "same-street sold prices" in payload["basis"]
    assert "(UK HPI, warwick)" in payload["basis"]


# ---------------------------------------------------------------------------
# 3 · Honest refusals: no town resolved, or a pool that cannot verifiably
#     reach the subject, yields NO tag (the abstention differentiator).
# ---------------------------------------------------------------------------

def test_unplaceable_subject_is_refused_not_tagged():
    payload = _value({"fields": {
        "address": "Nowhere Lane, ATLANTIS", "beds": 2, "sqft": 800,
        "price": 300000, "mode": "buy"}})
    assert "warmed town" in payload["error"]
    assert "tag" not in payload
    assert "hint" in payload                     # names the next step


def test_pool_that_cannot_reach_the_subject_is_refused():
    """A London-resolving subject whose outcode NO cached comp shares (the
    Battersea-vs-De-Beauvoir case) gets the reach refusal, not a tag."""
    payload = _value({"fields": {
        "address": "Sample Rise, London", "postcode": "SW11 9AA", "beds": 2,
        "sqft": 800, "price": 700000, "mode": "buy"}})
    assert "verifiably reach" in payload["error"]
    assert "tag" not in payload


def test_in_outcode_cold_street_still_earns_an_area_verdict():
    """The guard must not over-refuse: a cold street inside a cached outcode
    has real area evidence and keeps its verdict."""
    payload = _value({"fields": {
        "address": "Sample Close, London N1", "postcode": "N1 4AB", "beds": 2,
        "sqft": 800, "price": 800000, "mode": "buy"}})
    assert payload.get("error") is None
    assert payload["tag"] in ("steal", "fair", "over")


# ---------------------------------------------------------------------------
# 4 · The workings safety net stays covered: tools can no longer hand an
#     unverified pool to the trace, but show_work itself must keep tracing
#     one honestly (other callers pass arbitrary pools).
# ---------------------------------------------------------------------------

def test_workings_still_traces_an_unverified_pool_honestly():
    from gaff_engine import workings
    listing = {"address": {"display": "Sample Street, ELSEWHERE",
                           "outcode": "ZZ9"}}
    comps = [{"address": {"street": "NORTHCHURCH ROAD", "postcode": "N1 4EJ",
                          "town": "LONDON"}, "price": 800000}]
    work = workings.show_work(listing, verdict=None, comps=comps)
    assert work["addressMatch"]["matchLevel"] == "pool"
    assert "NOT verified" in workings.render_text(work)


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
