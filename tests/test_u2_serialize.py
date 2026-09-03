"""U2 serialization tests — the engine → JSON seam, checked against the oracle.

Runnable two ways (like tests/test_u1_golden.py):

    python3 -m pytest tests/test_u2_serialize.py -v     # if pytest is installed
    python3 tests/test_u2_serialize.py                  # plain-stdlib fallback

The plain-run path (the __main__ block) executes every ``test_*`` function with
the same asserts and prints PASS/FAIL, so U2 verifies with zero third-party deps.

What it pins: ``to_jsonable(GOLDEN_SCORE_RESULT)`` is a lossless, JSON-native
mirror of the golden verdict — it round-trips through ``json.dumps``/``loads``
unchanged, the load-bearing numbers survive intact, and every Enum has collapsed
to its wire string with no ``Enum`` object left anywhere in the tree.
"""

import json
import os
import sys
from enum import Enum

# Make the repo root importable whether run by pytest (from root) or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.schemas import ValueTag  # noqa: E402
from gaff_engine.serialize import score_result_to_json, to_jsonable  # noqa: E402
from gaff_engine.stub import stub_score  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import (  # noqa: E402
    GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SCORE_RESULT, GOLDEN_SEARCH,
)


def test_round_trips_through_json_without_loss():
    """to_jsonable(golden) is JSON-native and survives dumps→loads unchanged."""
    d = to_jsonable(GOLDEN_SCORE_RESULT)
    # It must be directly serializable with no default= hook.
    text = json.dumps(d)
    reloaded = json.loads(text)
    assert reloaded == d, "round-trip changed the structure"
    # And the pretty wrapper produces the same object when parsed back.
    assert json.loads(score_result_to_json(GOLDEN_SCORE_RESULT)) == d


def test_key_values_survive():
    """The load-bearing numbers are present and exact after serialization."""
    d = to_jsonable(GOLDEN_SCORE_RESULT)
    assert d["composite"] == 7.8, d["composite"]
    assert d["taste"]["score"] == 8.2, d["taste"]["score"]
    vv = d["valueVerdict"]
    assert vv["tag"] == "fair", vv["tag"]
    assert vv["deltaPct"] == -8.2, vv["deltaPct"]
    assert vv["headlineDeltaPct"] == -11.7, vv["headlineDeltaPct"]
    assert vv["fairEstimate"] == 1302000, vv["fairEstimate"]
    assert vv["band"] == {"low": 1140000, "high": 1410000}, vv["band"]
    assert d["rules"]["score"] == 7.5, d["rules"]["score"]


def test_enums_become_their_string_values():
    """Every Enum collapses to its wire string; no Enum object remains anywhere."""
    d = to_jsonable(GOLDEN_SCORE_RESULT)

    # Spot-check representative enums across the tree.
    assert d["valueVerdict"]["tag"] == ValueTag.FAIR.value == "fair"
    assert d["taste"]["axisBreakdown"][0]["axis"] == "light_and_volume"
    assert d["flags"][0]["code"] == "short_lease"
    assert d["flags"][0]["severity"] == "serious"
    assert d["components"][0]["component"] == "value_verdict"
    assert d["components"][0]["availability"] == "ready"

    # The tag is a plain str, not a ValueTag member (even though ValueTag mixes in str).
    assert type(d["valueVerdict"]["tag"]) is str
    assert not isinstance(d["valueVerdict"]["tag"], Enum)

    # Exhaustive: walk the whole structure and assert nothing is still an Enum.
    def _no_enums(x, path="<root>"):
        assert not isinstance(x, Enum), "Enum left unserialized at %s: %r" % (path, x)
        if isinstance(x, dict):
            for k, v in x.items():
                assert not isinstance(k, Enum), "Enum key at %s: %r" % (path, k)
                _no_enums(v, "%s.%s" % (path, k))
        elif isinstance(x, list):
            for i, v in enumerate(x):
                _no_enums(v, "%s[%d]" % (path, i))
    _no_enums(d)


def test_trailing_underscore_keyword_rename_is_restored():
    """A dataclass field renamed for a Python keyword (from_) serializes as `from`."""
    from gaff_engine.fixtures.de_beauvoir import GOLDEN_COMPONENT_SPEC
    spec = to_jsonable(GOLDEN_COMPONENT_SPEC)
    first_input = spec["inputs"][0]
    assert "from" in first_input and "from_" not in first_input, first_input
    assert first_input["from"] == "score.result", first_input


def test_stub_feeds_the_serializer_end_to_end():
    """The M0 seam: stub_score(inputs) → ScoreResult → JSON carries the verdict.

    Proves the stub returns the golden result and that the serializer consumes the
    stub's output (the exact hand-off build_m0.py performs), inputs ignored.
    """
    sr = stub_score(GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH)
    assert sr is GOLDEN_SCORE_RESULT
    d = json.loads(score_result_to_json(sr))
    assert d["composite"] == 7.8
    assert d["valueVerdict"]["tag"] == "fair"
    assert d["valueVerdict"]["fairEstimate"] == 1302000


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
