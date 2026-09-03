"""spec 10 §6 gate — weight-learning recovers a known taste, cold-starts to the
prior, and nudges early / dominates late. Deterministic (seeded). No network."""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gaff_engine.swipe import learn_weights, AXES


def _dist(a, b):
    return sum(abs(float(a.get(k, 0)) - float(b.get(k, 0))) for k in AXES)


def _person(weights):
    s = sum(weights.get(a, 0.0) for a in AXES) or 1.0
    return {a: weights.get(a, 0.0) / s for a in AXES}


def _swipes(true, k, seed):
    rng = random.Random(seed)
    h = []
    for _ in range(k):
        reads = {a: round(rng.uniform(1, 10), 1) for a in AXES}
        target = sum(reads[a] * true[a] for a in AXES)   # weighted avg, 0-10
        h.append((reads, target))
    return h


def test_recovers_known_weights():
    true = _person({"character_bones": 0.34, "design_finish": 0.26,
                    "light_and_volume": 0.20, "outdoor_space": 0.12,
                    "raw_size_threshold": 0.08})
    history = _swipes(true, 15, seed=42)
    prior = {a: 1.0 / len(AXES) for a in AXES}          # knows nothing (hardest)
    learned = learn_weights(history, prior, n=len(history))
    d_prior, d_learned = _dist(true, prior), _dist(true, learned)
    assert abs(sum(learned.values()) - 1.0) < 0.02, sum(learned.values())  # 2dp rounding
    assert d_learned < d_prior * 0.55, (d_prior, d_learned, learned)
    assert set(sorted(AXES, key=lambda a: -learned[a])[:2]) == {"character_bones", "design_finish"}, learned
    print("PASS recover: dist uniform %.3f -> learned %.3f; top2 %s"
          % (d_prior, d_learned, sorted(AXES, key=lambda a: -learned[a])[:2]))


def test_cold_start_returns_prior():
    prior = {a: (2.0 if a == "character_bones" else 1.0) for a in AXES}
    out = learn_weights([], prior)
    s = sum(prior.values())
    assert abs(out["character_bones"] - prior["character_bones"] / s) < 0.01, out
    print("PASS cold-start = prior")


def test_early_nudges_late_dominates():
    true = _person({"design_finish": 0.5, "light_and_volume": 0.3, "outdoor_space": 0.2})
    prior = {a: 1.0 / len(AXES) for a in AXES}
    w2 = learn_weights(_swipes(true, 2, seed=7), prior, n=2)
    w15 = learn_weights(_swipes(true, 15, seed=7), prior, n=15)
    assert _dist(true, w15) < _dist(true, w2), (_dist(true, w2), _dist(true, w15))
    print("PASS n=2 dist %.3f > n=15 dist %.3f (decaying prior pull)"
          % (_dist(true, w2), _dist(true, w15)))


if __name__ == "__main__":
    test_recovers_known_weights()
    test_cold_start_returns_prior()
    test_early_nudges_late_dominates()
    print("test_profiler OK")
