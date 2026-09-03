"""U18 tests — minimal elicitation (04-elicitation): a few answers → person@1.

    python3 -m pytest tests/test_u18_elicit.py -v
    python3 tests/test_u18_elicit.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.elicit import (  # noqa: E402
    AXES, _weights_from_priorities, person_from_answers, person_from_profile,
)
from gaff_engine.engine import score  # noqa: E402
from gaff_engine.taste import canonical_model  # noqa: E402
from gaff_engine.schemas import Person  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_SEARCH  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ANSWERS = {
    "name": "Finn", "household": "sharers", "minBeds": 2, "minBaths": 2, "minSqft": 900,
    "outdoorRequired": True, "narrationTone": "plain",
    "tastePriorities": ["light_and_volume", "outdoor_space", "character_bones"],
    "lovesNamed": ["skylit kitchens"],
}


# ---------------------------------------------------------------------------
# 1 · A handful of answers → a schema-valid Person.
# ---------------------------------------------------------------------------

def test_person_from_answers_is_valid():
    p = person_from_answers(_ANSWERS)
    assert isinstance(p, Person) and p.subject == "Finn"
    assert validate(p) == []
    # the hard constraints came straight from the answers.
    assert p.taste.hardConstraintsDefault["minBeds"] == 2
    assert p.values["narrationTone"] == "plain"


def test_person_is_deterministic():
    a, b = person_from_answers(_ANSWERS), person_from_answers(_ANSWERS)
    assert a.id == b.id                              # id derived from the name, no clock


# ---------------------------------------------------------------------------
# 2 · Weights come from the priority ranking (rank → weight, decaying).
# ---------------------------------------------------------------------------

def test_weights_from_priorities_rank_order():
    p = person_from_answers(_ANSWERS)
    w = p.taste.weights
    assert set(w) == set(AXES)                       # all eight present
    # the three ranked axes take the top of the scale, in the ranked order.
    assert w["light_and_volume"] == 10.0
    assert w["outdoor_space"] == 9.0
    assert w["character_bones"] == 8.5
    # an unranked axis sits at the floor.
    assert w["station_proximity"] == 0.5


def test_reordering_priorities_reorders_weights():
    w = _weights_from_priorities(["outdoor_space", "light_and_volume"])
    assert w["outdoor_space"] == 10.0 and w["light_and_volume"] == 9.0


# ---------------------------------------------------------------------------
# 3 · The elicited Person drives the real engine.
# ---------------------------------------------------------------------------

def test_elicited_person_scores_a_listing():
    p = person_from_answers(_ANSWERS)
    r = score(GOLDEN_LISTING, p, GOLDEN_SEARCH, taste_model=canonical_model())
    assert r.composite is not None and 0 <= r.composite <= 10
    assert r.taste is not None and r.valueVerdict is not None


# ---------------------------------------------------------------------------
# 4 · The fuller path — profile.json v3 → Person.
# ---------------------------------------------------------------------------

def test_person_from_profile_valid():
    with open(os.path.join(_ROOT, "profile.json")) as f:
        profile = json.load(f)
    p = person_from_profile(profile)
    assert validate(p) == []
    assert p.taste.weights and len(p.taste.weights) == 8
    assert p.taste.lovesNamed                         # the named loves came through


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
