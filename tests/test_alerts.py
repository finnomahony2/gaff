"""U-alert tests — the P8 alerts delivery engine (08-action.md §5.4 / A8-A10).

Pure functions over synthetic score.result@1 + policy: the only-surface-7-plus
gate, the protective (saved-from-a-mistake) alert, and the digest's cadence /
cap / overflow / quiet-hours / ranking.

    python3 -m pytest tests/test_alerts.py -v
    python3 tests/test_alerts.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.alerts import ALERT_CONFIG, assemble_digest, evaluate_alert  # noqa: E402
from gaff_engine.schemas import (  # noqa: E402
    AlertPolicy, AlertState, AlertType, BuyDetails, Flag, Listing, Money, Ref,
    RulesResult, ScoreResult, Search, Threshold, ValueVerdict,
)
from gaff_engine.validate import validate  # noqa: E402

DAY = {"from": "2026-07-13T07:30:00Z", "to": "2026-07-14T07:30:00Z"}


def _search(min_comp=None, cadence="daily", quiet="22:00-07:00", cap=8, channel="email"):
    return Search(id="search_buy", personRef=Ref(id="person_finn", schemaVersion="person@1"),
                  threshold=Threshold(show=6.0, alert=7.5),
                  alertPolicy=AlertPolicy(channel=channel, cadence=cadence, minComposite=min_comp,
                                          maxPerDigest=cap, quietHours=quiet))


def _score(composite, tag="fair", excluded=False, serious=(), sid="score_x"):
    return ScoreResult(id=sid, composite=(0.0 if excluded else composite),
                       rules=RulesResult(excluded=excluded),
                       valueVerdict=(None if excluded else ValueVerdict(tag=tag)),
                       flags=[Flag(code=c, severity="serious") for c in serious])


def _listing(price=1150000, lid="listing_deb", key="deb_key"):
    return Listing(id=lid, listingKey=key, buy=BuyDetails(price=Money(amount=price)))


# ---------------------------------------------------------------------------
# A8 — the only-surface-7-plus gate.
# ---------------------------------------------------------------------------

def test_gate_only_surfaces_7_plus():
    s = _search()
    e = evaluate_alert(_score(7.8), _listing(), s)                   # a fresh 7.8 clears
    assert e.type == AlertType.NEW_MATCH and e.state == AlertState.PENDING
    assert e.gate.passed and e.gate.minComposite == 7.5
    assert validate(e) == [], validate(e)

    e2 = evaluate_alert(_score(6.9), _listing(key="k2"), s)          # the 6.9 stays in-app
    assert e2.state == AlertState.SUPPRESSED and e2.suppressReason == "below_min_composite"

    e3 = evaluate_alert(_score(0, excluded=True), _listing(key="k3"), s)  # excluded never alerts
    assert e3.state == AlertState.SUPPRESSED and e3.suppressReason == "excluded"
    assert e3.gate.passed is False


def test_dedupe_suppresses_a_repeat():
    s = _search()
    seen = set()
    e = evaluate_alert(_score(7.8), _listing(key="dk"), s, seen_keys=seen)
    assert e.state == AlertState.PENDING
    again = evaluate_alert(_score(7.9), _listing(key="dk"), s, seen_keys=seen)
    assert again.state == AlertState.SUPPRESSED and again.suppressReason == "dedupe"


# ---------------------------------------------------------------------------
# A9 — the protective (saved-from-a-mistake) alert.
# ---------------------------------------------------------------------------

def test_protective_bypasses_gate_but_holds_quiet_hours():
    s = _search()
    prior = {"saved": True, "tag": "fair", "price": 1150000, "pursuitId": "pursuit_deb"}
    # a shortlisted home re-scored `over` at composite 6.5 (BELOW the gate) still alerts
    e = evaluate_alert(_score(6.5, tag="over"), _listing(), s, prior=prior, now="2026-07-13T23:00:00Z")
    assert e.type == AlertType.VERDICT_CHANGE
    assert e.state == AlertState.PENDING and e.gate.protectiveBypass is True
    assert e.pursuitRef is not None
    # ...but delivered only after quiet hours (23:00 → next 07:00), never dropped
    d = assemble_digest([e], s, window={"from": "2026-07-13T07:30:00Z", "to": "2026-07-13T23:00:00Z"})
    assert d.sentAt == "2026-07-14T07:00:00Z"

    # a NEW serious flag on a saved listing is also protective
    prior2 = {"saved": True, "tag": "fair", "price": 1150000, "seriousFlagCodes": []}
    e2 = evaluate_alert(_score(7.0, serious=["structural_movement"]), _listing(), s, prior=prior2)
    assert e2.type == AlertType.VERDICT_CHANGE and e2.gate.protectiveBypass is True


def test_price_drop_trigger_carries_the_delta():
    s = _search()
    prior = {"saved": True, "tag": "fair", "price": 1195000}
    e = evaluate_alert(_score(7.8), _listing(price=1150000), s, prior=prior)
    assert e.type == AlertType.PRICE_DROP and e.state == AlertState.PENDING  # saved → already past gate
    assert e.trigger["was"]["price"] == 1195000 and e.trigger["now"]["price"] == 1150000
    assert e.trigger["delta"] == -45000


# ---------------------------------------------------------------------------
# A10 — the digest: rank, cap, roll over, quiet hours.
# ---------------------------------------------------------------------------

def test_digest_ranks_caps_and_rolls_over_never_drops():
    s = _search(cap=2)                                # tiny cap to force overflow
    events = [evaluate_alert(_score(comp), _listing(key="k%d" % i, lid="l%d" % i), s)
              for i, comp in enumerate([7.6, 8.1, 7.9, 7.7])]
    d = assemble_digest(events, s, window=DAY)
    assert d.count == 2 and d.capped is True and len(d.rolledOver) == 2
    # ranked composite desc → 8.1 (k1) then 7.9 (k2) kept
    assert [r.id for r in d.items] == ["alert_new_match_k1", "alert_new_match_k2"]
    assert d.sentAt == "2026-07-14T07:30:00Z"        # 07:30 is outside quiet hours
    rolled_ids = {r.id for r in d.rolledOver}
    assert all(e.state == AlertState.PENDING for e in events if e.id in rolled_ids)  # never dropped
    assert validate(d) == [], validate(d)


def test_reproduces_the_5_4a_daily_digest():
    s = _search()                                    # cap 8, daily, email
    price_drop = evaluate_alert(_score(7.8), _listing(price=1150000, key="deb"), s,
                                prior={"saved": True, "tag": "fair", "price": 1195000})
    new_match = evaluate_alert(_score(7.6), _listing(price=999000, key="new1", lid="l_new"), s)
    noise = evaluate_alert(_score(6.9), _listing(key="noise", lid="l_noise"), s)  # the 6.9 held back
    d = assemble_digest([price_drop, new_match, noise], s, window=DAY)
    assert d.count == 2 and d.capped is False        # both alerts in; the 6.9 excluded
    assert noise.state == AlertState.SUPPRESSED
    assert [r.id for r in d.items] == ["alert_price_drop_deb", "alert_new_match_new1"]  # 7.8 before 7.6
    assert "price drop" in d.narration["headline"].lower()
    assert d.channel == "email"


def test_protective_sorts_to_the_top_of_the_digest():
    s = _search()
    high = evaluate_alert(_score(8.4), _listing(key="high", lid="l_high"), s)
    prot = evaluate_alert(_score(6.5, tag="over"), _listing(key="prot", lid="l_prot"), s,
                          prior={"saved": True, "tag": "fair", "price": 900000})
    d = assemble_digest([high, prot], s, window=DAY)
    # protective ranks above the higher-composite new match (protecting the decision outranks fit)
    assert [r.id for r in d.items][0] == "alert_verdict_change_prot"


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
