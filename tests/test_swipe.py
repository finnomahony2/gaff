"""U-swipe tests — the P4 learning engine (04-elicitation.md §5), pinned to the
spec's worked numbers and acceptance criteria (A1, A2, A4, A5, A10) plus the
single-observation trust cap and the twin decay.

    python3 -m pytest tests/test_swipe.py -v
    python3 tests/test_swipe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.swipe import (  # noqa: E402
    AXES, Feedback, Observation, apply_feedback, build_twin, clarity,
    expected_info_gain, seed_uncertainty, select_next_probe, twin_weight,
)
from gaff_engine.schemas import AxisBelief, Probe, ProbeKind  # noqa: E402
from gaff_engine.elicit import person_from_answers  # noqa: E402


def _person():
    # No tastePriorities → the canonical weights: light 10 · outdoor 9 · character
    # 8.5 · width 8 · street 8 · size 6 · finish 4 · station 0.5 (the §5.2 fixture).
    return person_from_answers({"name": "Finn"})


# ---------------------------------------------------------------------------
# Seed + clarity (§5.2 / A10).
# ---------------------------------------------------------------------------

def test_seed_is_flat_and_clarity_zero():
    unc = seed_uncertainty(_person())
    assert unc.overall.clarity0to1 == 0.0                 # every σ = σ0 → clarity 0
    assert all(unc.axes[a].sigma == 3.0 for a in AXES)
    assert unc.provenance.seededFromTwin is False


def test_clarity_matches_the_5_2_fixture():
    person = _person()
    unc = seed_uncertainty(person)
    fixture = {"light_and_volume": (8.6, 1.10, 7), "outdoor_space": (8.9, 1.05, 8),
               "character_bones": (8.7, 1.00, 9), "width_proportion_flow": (8.1, 1.30, 5),
               "street_scene": (7.4, 2.60, 1), "raw_size_threshold": (6.2, 1.70, 4),
               "design_finish": (4.2, 1.50, 6), "station_proximity": (3.0, 2.90, 0)}
    for a, (m, s, n) in fixture.items():
        unc.axes[a] = AxisBelief(mean=m, sigma=s, nObs=n)
    assert clarity(unc, person.taste.weights) == 0.68     # the spec's computed value


# ---------------------------------------------------------------------------
# applyFeedback — the Petherton numeric fixture (§5.5 / A4).
# ---------------------------------------------------------------------------

def test_petherton_right_swipe_reproduces_the_spec_numbers():
    person = _person()
    unc = seed_uncertainty(person)
    unc.axes["width_proportion_flow"] = AxisBelief(mean=6.0, sigma=3.0, nObs=0)   # the §5.5 prior
    fb = Feedback(kind="swipe", direction="right", primaryAxis="width_proportion_flow",
                  observations=[Observation("width_proportion_flow", 9.5, 0.8)],
                  namedLoves=["double-fronted width"], archetypeTier="S", area="Highbury")
    p2, unc2, receipt, interp = apply_feedback(person, fb, unc)

    b = unc2.axes["width_proportion_flow"]
    assert b.sigma == 1.30 and b.mean == 8.1              # σ 3.0→1.30, mean 6.0→8.1
    assert p2.profile.version == person.profile.version + 1
    assert interp["axis"] == "width_proportion_flow" and interp["signalDelta"] == 2.1
    assert "σ 3.00, mean 6.0" in receipt.before and "σ 1.30, mean 8.1" in receipt.after
    assert "double-fronted width" in (p2.taste.lovesNamed or [])
    assert unc2.archetypeCoverage["S"] == 1
    assert unc2.areaAffinity["Highbury"]["leaning"] == "like"
    # purity — the inputs are untouched
    assert unc.axes["width_proportion_flow"].sigma == 3.0
    assert person.profile.version == 3


def test_single_observation_trust_cap_and_contradiction():
    person = _person()
    unc = seed_uncertainty(person)
    unc.axes["light_and_volume"] = AxisBelief(mean=6.0, sigma=3.0, nObs=0)
    up = Feedback(direction="right", primaryAxis="light_and_volume",
                  observations=[Observation("light_and_volume", 10.0, 0.8)])
    p1, u1, _, _ = apply_feedback(person, up, unc)
    # one enthusiastic swipe moves mean at most 0.6·(10−6)=2.4 → 8.4, not to ~9.7
    assert u1.axes["light_and_volume"].mean == 8.4
    # a contradicting later swipe corrects it back down
    down = Feedback(direction="left", primaryAxis="light_and_volume",
                    observations=[Observation("light_and_volume", 3.0, 0.8)])
    _, u2, _, _ = apply_feedback(p1, down, u1)
    assert u2.axes["light_and_volume"].mean < u1.axes["light_and_volume"].mean


# ---------------------------------------------------------------------------
# Two mentions confirm an anti-signal (§5.5 / A5).
# ---------------------------------------------------------------------------

def test_two_mentions_confirm_the_marble_anti_signal():
    person = _person()
    unc = seed_uncertainty(person)
    fb = Feedback(kind="correction", observations=[], primaryAxis="design_finish",
                  antiSignalMentions=[("marble finishes", -1.0, False)])
    p1, u1, _, i1 = apply_feedback(person, fb, unc)
    assert u1.antiSignals["marble finishes"].mentions == 1
    assert u1.antiSignals["marble finishes"].confirmed is False
    assert not any(a.signal == "marble finishes" for a in (p1.taste.antiSignals or []))

    p2, u2, r2, i2 = apply_feedback(p1, fb, u1)
    assert u2.antiSignals["marble finishes"].confirmed is True
    anti = [a for a in p2.taste.antiSignals if a.signal == "marble finishes"][0]
    assert anti.penalty == -1.0 and anti.fatal is False
    assert i2["newAntiSignal"] == "marble finishes"
    # the exact P1 receipt (A5): a standing, every-Search dislike, two mentions
    assert r2.scope == "every search"
    assert r2.summary.startswith("Marble is now a standing dislike across every Search.")
    assert "Two mentions" in r2.summary and "made it stick" in r2.summary


# ---------------------------------------------------------------------------
# selectNextProbe — EIG argmax (§5.3 / A2) + value-before-ask (§5.1 / A1).
# ---------------------------------------------------------------------------

def test_eig_reproduces_the_worked_case():
    person = _person()
    unc = seed_uncertainty(person)                        # all σ = 3.0
    informs3 = [{"target": "axis", "key": "light_and_volume"},
                {"target": "axis", "key": "outdoor_space"},
                {"target": "axis", "key": "character_bones"}]
    swipe = Probe(kind=ProbeKind.SWIPE_CARD, valuePayload={"home": "brownlow"}, informs=informs3)
    oneword = Probe(kind=ProbeKind.ONE_WORD, valuePayload={"home": "x"},
                    informs=[{"target": "axis", "key": "design_finish"}])
    voice = Probe(kind=ProbeKind.VOICE_RATE, valuePayload={"home": "brownlow"}, informs=informs3)
    w = person.taste.weights
    assert expected_info_gain(swipe, unc, w) == 18.20     # a rich multi-axis swipe
    assert expected_info_gain(oneword, unc, w) == 2.34    # ~8× less than the swipe
    assert expected_info_gain(voice, unc, w) == 23.11     # voice is the richest input
    assert select_next_probe(unc, [swipe, oneword], person) is swipe
    assert select_next_probe(unc, [oneword, voice, swipe], person) is voice


def test_value_before_ask_drops_bare_asks():
    person = _person()
    unc = seed_uncertainty(person)
    bare = Probe(kind=ProbeKind.ONE_WORD, valuePayload=None,
                 informs=[{"target": "axis", "key": "design_finish"}])
    valued = Probe(kind=ProbeKind.SWIPE_CARD, valuePayload={"home": "x"},
                   informs=[{"target": "axis", "key": "light_and_volume"}])
    assert select_next_probe(unc, [bare], person) is None        # a bare ask is infeasible
    assert select_next_probe(unc, [bare, valued], person) is valued


def test_anti_fatigue_moves_the_deck_off_a_hammered_axis():
    person = _person()
    unc = seed_uncertainty(person)
    light = Probe(kind=ProbeKind.SWIPE_CARD, valuePayload={"h": 1},
                  informs=[{"target": "axis", "key": "light_and_volume"}])
    outdoor = Probe(kind=ProbeKind.SWIPE_CARD, valuePayload={"h": 2},
                    informs=[{"target": "axis", "key": "outdoor_space"}])
    recent = [["light_and_volume"], ["light_and_volume"], ["light_and_volume"]]
    # light (w10) normally beats outdoor (w9), but three recent light probes penalise it
    assert select_next_probe(unc, [light, outdoor], person, recent_axes=recent) is outdoor


# ---------------------------------------------------------------------------
# Cold start — the Taste-twin decays fast + refuses below the k-anon floor (§5.9 / A9).
# ---------------------------------------------------------------------------

def test_twin_decays_and_refuses_below_cohort_floor():
    assert round(twin_weight(2.6, 0.8, 1), 2) == 0.09    # after 1 obs
    assert twin_weight(2.6, 0.8, 4) < 0.03               # ~0.02: essentially gone
    assert build_twin({"lifeStage": "couple", "city": "London"},
                      {a: (6.0, 2.6) for a in AXES}, 12) is None      # sub-floor → no twin
    assert build_twin({"lifeStage": "couple", "city": "London"},
                      {a: (6.0, 2.6) for a in AXES}, 612) is not None  # valid cohort


def test_swipe_feedback_maps_every_gesture_to_a_mutation():
    """A3 — no gesture is a no-op. swipe_feedback decomposes a real card via the P3
    model into salient-axis observations, signed by gesture."""
    from gaff_engine.swipe import swipe_feedback
    from gaff_engine.fixtures.shortlist import swipe_deck
    person = _person()
    unc = seed_uncertainty(person)
    deck = swipe_deck()

    # a right swipe informs only its salient axes (not all eight), lifts clarity
    right = swipe_feedback(deck[0]["listing"], person, deck[0]["taste_model"], "right", archetype_tier="S")
    assert right.direction == "right" and 1 <= len(right.observations) <= 4
    p2, u2, _, _ = apply_feedback(person, right, unc)
    assert u2.overall.clarity0to1 > unc.overall.clarity0to1
    assert p2.profile.version == person.profile.version + 1

    # a left with a named offending axis → that axis takes the low 'skinny=kill' obs
    yeate = next(c for c in deck if c["offending_axis"] == "width_proportion_flow")
    left = swipe_feedback(yeate["listing"], person, yeate["taste_model"], "left",
                          offending_axis="width_proportion_flow")
    wobs = [o for o in left.observations if o.axis == "width_proportion_flow"]
    assert wobs and wobs[0].value <= 2.5

    # a left carrying an anti-signal surfaces the mention (the flip), not an axis kill
    grey_flip = next(c for c in deck if c["listing"].id == "listing_demo_debeauvoir")
    flip = swipe_feedback(grey_flip["listing"], person, grey_flip["taste_model"], "left")
    assert any("cheap" in s or "careless" in s for s, _, _ in flip.antiSignalMentions)


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
