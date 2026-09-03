"""U7-live tests — the LiveForensicsModel adapter seam (M4).

DETERMINISTIC: the vision boundary (``vision_fn``) is a recorded replay. A live
vision read needs the actual photos + a vision model (production); this suite
pins the adapter + the request/response contract offline.

    python3 -m pytest tests/test_forensics_live.py -v
    python3 tests/test_forensics_live.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.forensics_live import (  # noqa: E402
    LiveForensicsModel, build_vision_request, parse_vision_response, replay_vision_model,
)
from gaff_engine.forensics import fatal_anti_signals, forensics_flags  # noqa: E402
from gaff_engine.schemas import Forensics  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import GOLDEN_FORENSICS, GOLDEN_LISTING  # noqa: E402


def _to_response(f):
    return {"roomWidthsM": f.roomWidthsM, "walkThroughBedroom": f.walkThroughBedroom,
            "hmoTells": f.hmoTells, "cheapFlipSignals": f.cheapFlipSignals,
            "aspect": f.aspect, "ceilingHeightCue": f.ceilingHeightCue, "floorPosition": f.floorPosition}


def test_vision_request_marks_listing_text_untrusted():
    """T12: keyFeatures and image-readable text are listing-authored data."""
    from gaff_engine.forensics_live import build_vision_request
    from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING as _GL
    req = build_vision_request(_GL)
    assert "UNTRUSTED" in req["contextNote"]


def test_build_vision_request_carries_images():
    req = build_vision_request(GOLDEN_LISTING)
    assert req["schemaVersion"] == "forensics.read.request@1"
    assert req["imageUrls"] and req["floorplanUrls"]        # the model looks at these
    assert set(req["context"]) >= {"propertyType", "beds", "sqft"}


def test_live_adapter_reproduces_golden_forensics():
    rec = {GOLDEN_LISTING.listingKey: _to_response(GOLDEN_FORENSICS)}
    live = LiveForensicsModel(replay_vision_model(rec))
    f = live(GOLDEN_LISTING)
    assert isinstance(f, Forensics)
    assert f.aspect == "south-west (rear)" and f.floorPosition == "raised + lower ground"
    assert [x.code.value for x in forensics_flags(f, GOLDEN_LISTING)] == ["lower_ground_light"]
    assert fatal_anti_signals(f) == []


def test_parse_cheap_flip_is_fatal():
    f = parse_vision_response(
        {"cheapFlipSignals": ["grey landlord refurb", "laminate"], "hmoTells": True,
         "aspect": "north-facing"}, GOLDEN_LISTING)
    codes = [x.code.value for x in forensics_flags(f)]
    assert "cheap_careless_spec" in codes and "north_facing" in codes and "hmo_history" in codes
    assert fatal_anti_signals(f)[0][2] is True              # feeds taste the fatal cap


def test_parse_conservative_defaults():
    """A sparse response never invents a kill — no flip, no HMO by default (§5.5)."""
    f = parse_vision_response({"aspect": "east (rear)"}, GOLDEN_LISTING)
    assert f.cheapFlipSignals == [] and f.hmoTells is False
    assert fatal_anti_signals(f) == []


def test_live_model_is_drop_in_for_the_engine():
    from gaff_engine.engine import score
    from gaff_engine.taste import canonical_model
    from gaff_engine.fixtures.de_beauvoir import GOLDEN_PERSON, GOLDEN_SEARCH
    rec = {GOLDEN_LISTING.listingKey: _to_response(GOLDEN_FORENSICS)}
    r = score(GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH,
              taste_model=canonical_model(), forensics_model=LiveForensicsModel(replay_vision_model(rec)))
    assert any(fl.code.value == "lower_ground_light" for fl in r.flags)
    assert r.composite == 7.6


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
