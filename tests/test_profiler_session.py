"""spec 10 §6 behavioural gate (deterministic proxy for the real cross-person run):
a synthetic rater with a known taste calibrates on the real cohort, and the engine's
held-out prediction with LEARNED weights beats the same engine with FROZEN weights.
Offline (replays the cached vision reads); skips if the cohort data isn't present."""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gaff_engine.swipe import AXES
from gaff_engine.taste import taste_result
from gaff_engine import profiler


def _norm(w):
    s = sum(w.get(a, 0.0) for a in AXES) or 1.0
    return {a: w.get(a, 0.0) / s for a in AXES}


def _true_score(reads, w):
    return sum(float(reads.get(a, 5.0)) * w[a] for a in AXES)   # w sums to 1


def _reaction(sc):
    return "love" if sc >= 8 else "like" if sc >= 6.5 else "meh" if sc >= 4 else "dislike"


def test_calibration_beats_frozen():
    if not (os.path.exists(profiler.VISION) and os.path.exists(profiler.COHORT)):
        print("SKIP test_calibration_beats_frozen (cohort data not present)")
        return
    c = profiler.load_profiling_cohort()
    true = _norm({"character_bones": 0.40, "design_finish": 0.30,
                  "light_and_volume": 0.15, "outdoor_space": 0.15})

    s = profiler.ProfilerSession(c, name="synthetic", stop_reactions=15, seed=42)
    while not s.calibrated():
        nh = s.next_home()
        if not nh:
            break
        s.react(nh["id"], _reaction(_true_score(c["reads_by_id"][nh["id"]], true)))
    for hid in s.holdout:
        s.record_blind(hid, _true_score(c["reads_by_id"][hid], true))

    learned = s.predict()
    assert learned["n"] >= 2, learned

    # frozen baseline: identical Person, weights reset to the uniform cold-start prior
    frozen = copy.deepcopy(s.person)
    frozen.taste.weights = {a: 1.0 / len(AXES) for a in AXES}
    errs = [abs(taste_result(c["listings"][hid], frozen, c["model"]).score - s.blind[hid])
            for hid in s.holdout if hid in s.blind]
    mae_frozen = sum(errs) / len(errs)

    assert learned["mae"] < mae_frozen, (learned["mae"], mae_frozen)
    top = [a for a, _ in s.read()["topAxes"][:2]]
    assert set(top) == {"character_bones", "design_finish"}, top
    print("PASS learned MAE %.2f < frozen %.2f; recovered top2 %s in %d reactions"
          % (learned["mae"], mae_frozen, top, len(s.history)))


if __name__ == "__main__":
    test_calibration_beats_frozen()
    print("test_profiler_session OK")
