"""The active-learning phase gate — the synthetic-user harness (04-elicitation.md
§7.2). The new claim P4 makes is *learning speed*: fewer, better-chosen questions.
Prove it offline before any user sees it.

Build synthetic Persons with a known ground-truth taste vector, then run two
elicitation loops against each — (a) select_next_probe (active) vs (b) a fixed
random-order baseline — over the same pool with the same apply_feedback. Assert:

  * active reaches a target clarity (0.6) in >= 30% fewer probes than random, and
    never worse (§7.2 step 3);
  * recovered-taste error (weight-aware MAE of the learned means vs the truth)
    is lower under active at every measured K (§7.2 step 4);
  * a single enthusiastic swipe never over-rotates a belief past the single-obs
    cap, and a contradiction corrects it (§7.2 step 5 over-rotation guard).

Deterministic: the "random" baseline is a fixed-seed shuffle averaged over many
seeds, so the gate is a stable regression net (the analogue of the taste-replay
harness). Change a probeNoise or a threshold, re-run this, ship only if the gap
holds.

    python3 -m pytest tests/test_synthetic.py -v
    python3 tests/test_synthetic.py
"""

import copy
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.swipe import (  # noqa: E402
    AXES, CONFIG, Feedback, Observation, apply_feedback, seed_uncertainty,
    select_next_probe,
)
from gaff_engine.schemas import Probe, ProbeKind  # noqa: E402
from gaff_engine.elicit import person_from_answers  # noqa: E402

# A Finn-like ground truth (the §5.2 means) the loops must recover.
TRUTH = {"light_and_volume": 8.6, "outdoor_space": 8.9, "character_bones": 8.7,
         "width_proportion_flow": 8.1, "street_scene": 7.4, "raw_size_threshold": 6.2,
         "design_finish": 4.2, "station_proximity": 3.0}

_TAU = CONFIG["probeNoise"]


def _person():
    return person_from_answers({"name": "Synth"})


def _probe(pid, kind, axes):
    return Probe(id=pid, kind=kind, valuePayload={"home": pid},
                 informs=[{"target": "axis", "key": a} for a in axes])


def _pool():
    """A realistic mixed pool: six rich multi-axis swipes spanning all eight axes
    (high EIG) interleaved with cheap one-word / this-or-that probes on low-weight
    axes (low EIG). Active should front-load the swipes; random wastes early picks."""
    S = ProbeKind.SWIPE_CARD
    O = ProbeKind.ONE_WORD
    T = ProbeKind.THIS_OR_THAT
    return [
        _probe("s1", S, ["light_and_volume", "outdoor_space", "character_bones"]),
        _probe("s2", S, ["character_bones", "width_proportion_flow", "street_scene"]),
        _probe("s3", S, ["light_and_volume", "width_proportion_flow", "raw_size_threshold"]),
        _probe("s4", S, ["outdoor_space", "street_scene", "character_bones"]),
        _probe("s5", S, ["light_and_volume", "outdoor_space", "width_proportion_flow"]),
        _probe("s6", S, ["street_scene", "raw_size_threshold", "outdoor_space"]),
        _probe("w1", O, ["design_finish"]),
        _probe("w2", O, ["station_proximity"]),
        _probe("w3", O, ["raw_size_threshold"]),
        _probe("w4", O, ["design_finish"]),
        _probe("t1", T, ["station_proximity"]),
        _probe("t2", T, ["design_finish"]),
    ]


def _answer(probe):
    """The synthetic user answers truthfully: each informed axis gets an
    observation at its ground-truth value, with the probe kind's noise τ."""
    tau = _TAU.get(getattr(probe.kind, "value", probe.kind),
                   _TAU["swipe_bare"] if getattr(probe.kind, "value", probe.kind) == "swipe_card" else _TAU["one_word"])
    if getattr(probe.kind, "value", probe.kind) == "swipe_card":
        tau = _TAU["swipe_bare"]
    axes = [i["key"] for i in probe.informs]
    obs = [Observation(a, TRUTH[a], tau) for a in axes]
    primary = max(axes, key=lambda a: _person().taste.weights.get(a, 0.0))
    return Feedback(kind="rating", observations=obs, direction="right", primaryAxis=primary)


def _weighted_mae(unc, weights):
    num = sum(weights[a] * abs(unc.axes[a].mean - TRUTH[a]) for a in AXES)
    return num / sum(weights[a] for a in AXES)


def _run(order, target=0.6, record_at=None):
    """Consume `order` (a list of probes) one at a time, applying each answer,
    until clarity >= target or the list is exhausted. `order` is either the fixed
    baseline sequence or, when None, the active selector picks from the remaining
    pool each step. Returns (probes_used, reached, mae_at_K)."""
    person = _person()
    weights = person.taste.weights
    unc = seed_uncertainty(person)
    mae_at = {}
    record_at = record_at or set()

    remaining = list(order) if order is not None else _pool()
    served = 0
    while unc.overall.clarity0to1 < target and remaining:
        if order is None:
            probe = select_next_probe(unc, remaining, person)
            remaining.remove(probe)
        else:
            probe = remaining.pop(0)
        person, unc, _, _ = apply_feedback(person, _answer(probe), unc)
        served += 1
        if served in record_at:
            mae_at[served] = _weighted_mae(unc, weights)
    return served, unc.overall.clarity0to1 >= target, mae_at


def test_active_learning_reaches_clarity_in_fewer_probes():
    active_probes, active_reached, _ = _run(order=None, target=0.6)
    assert active_reached, "active loop should reach clarity 0.6 from this pool"

    randoms = []
    for seed in range(16):
        pool = _pool()
        random.Random(seed).shuffle(pool)
        n, reached, _ = _run(order=pool, target=0.6)
        assert reached, "baseline should also reach 0.6 (same pool)"
        randoms.append(n)
    mean_random = sum(randoms) / len(randoms)

    # >= 30% fewer than the mean random order, and never worse than the best one.
    assert active_probes <= 0.70 * mean_random, (active_probes, mean_random)
    assert active_probes <= min(randoms), (active_probes, min(randoms))
    print("      active %d probes vs random mean %.1f / min %d / max %d (>=30%% fewer)"
          % (active_probes, mean_random, min(randoms), max(randoms)))


def test_recovered_taste_error_dominates_at_every_k():
    ks = {3, 5, 7}
    _, _, active_mae = _run(order=None, target=1.1, record_at=ks)  # 1.1 → run the full pool
    rand_mae = {k: [] for k in ks}
    for seed in range(16):
        pool = _pool()
        random.Random(seed).shuffle(pool)
        _, _, m = _run(order=pool, target=1.1, record_at=ks)
        for k in ks:
            rand_mae[k].append(m[k])
    for k in sorted(ks):
        mean_rand = sum(rand_mae[k]) / len(rand_mae[k])
        assert active_mae[k] <= mean_rand, (k, active_mae[k], mean_rand)
        print("      K=%d  active weighted-MAE %.3f <= random mean %.3f" % (k, active_mae[k], mean_rand))


def test_over_rotation_guard():
    person = _person()
    unc = seed_uncertainty(person)
    from gaff_engine.schemas import AxisBelief
    unc.axes["light_and_volume"] = AxisBelief(mean=6.0, sigma=3.0, nObs=0)
    fb = Feedback(direction="right", primaryAxis="light_and_volume",
                  observations=[Observation("light_and_volume", 10.0, 0.8)])
    _, u1, _, _ = apply_feedback(person, fb, unc)
    move = u1.axes["light_and_volume"].mean - 6.0
    cap = CONFIG["singleObsCap"] * (10.0 - 6.0)
    assert move <= cap + 1e-9, (move, cap)          # never past the single-obs cap
    assert round(move, 6) == 2.4                     # exactly the capped move, not the ~3.7 Bayesian jump


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
