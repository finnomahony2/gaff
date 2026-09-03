"""spec 11 §11.1 — the pairwise ProfilerSession wiring, end to end on the real cohort.
Two synthetic personas duel through the session API (next_pair/react_pair) and must
produce differentiated shortlists + a contracting confidence. Offline; skips if the
cohort data is absent. The rating path is covered by test_profiler_session.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gaff_engine import preference as pref
from gaff_engine import profiler


def _modernist():
    w = [0.0] * pref.DIM
    w[pref.FEATURES.index("design_finish")] = 2.0
    w[pref.FEATURES.index("light_and_volume")] = 1.4
    w[pref.FEATURES.index("character_bones")] = 0.8
    w[pref.FEATURES.index(pref.INTERACTION)] = -2.6
    return w


def _purist():
    w = [0.0] * pref.DIM
    w[pref.FEATURES.index("character_bones")] = 2.0
    w[pref.FEATURES.index(pref.INTERACTION)] = 1.6
    w[pref.FEATURES.index("design_finish")] = 0.5
    return w


def _drive(c, w_true, budget=14):
    s = profiler.ProfilerSession(c, name="p", elicitation="pairwise",
                                 stop_reactions=budget, seed=0)
    conf0 = s.confidence()["overall0to1"]
    while not s.pairwise_calibrated():
        q = s.next_pair()
        if not q:
            break
        a, b = q["aId"], q["bId"]
        ua = pref.utility(w_true, s.pref_phi[a])
        ub = pref.utility(w_true, s.pref_phi[b])
        r = s.react_pair(a, b, "a" if ua >= ub else "b")
    return s, conf0, r


def test_pairwise_session_separates_personas():
    if not (os.path.exists(profiler.VISION) and os.path.exists(profiler.COHORT)):
        print("SKIP test_pairwise_session (cohort data not present)")
        return
    c = profiler.load_profiling_cohort()

    sm, c0m, rm = _drive(c, _modernist())
    sp, c0p, rp = _drive(c, _purist())

    # both produced a shortlist with taste + a confidence band, and duel homes excluded
    slm = sm.shortlist(mode="buy", k=6)
    slp = sp.shortlist(mode="buy", k=6)
    assert slm["rows"] and slp["rows"], "no shortlist rows"
    assert all(r["band"] is not None for r in slm["rows"]), "pairwise rows carry a band"
    assert not (set(sm.pref_seen) & {r["id"] for r in slm["rows"]}), "duel homes leaked into shortlist"

    # the purist ranks period character far higher than the modernist does
    def top_char(c, sl):
        return sum(c["reads_by_id"][r["id"]]["character_bones"] for r in sl["rows"][:3]) / 3
    cm, cp = top_char(c, slm), top_char(c, slp)
    assert cp >= cm + 1.0, "purist top-3 char %.1f should exceed modernist %.1f" % (cp, cm)

    # confidence contracts from the flat start as duels arrive (posterior tightened)
    assert rm["confidence"] >= c0m and rp["confidence"] >= c0p, "confidence should not fall"

    # the two personas resolve DIFFERENT leading axes
    tm = sm.confidence()["topAxes"][0]["axis"]
    tp = sp.confidence()["topAxes"][0]["axis"]
    print("PASS pairwise session: purist top-3 char %.1f > modernist %.1f | "
          "lead axes mod=%s pur=%s | conf mod %.2f pur %.2f"
          % (cp, cm, tm, tp, rm["confidence"], rp["confidence"]))


if __name__ == "__main__":
    test_pairwise_session_separates_personas()
    print("test_profiler_pairwise OK")
