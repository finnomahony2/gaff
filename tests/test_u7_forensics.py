"""U7 tests — the Forensics scorer (03-engine §5.5), the photo + floorplan read.

DETERMINISTIC: no vision LLM, no network. The vision judgement is injected as a
RecordedForensicsModel (the canonical De Beauvoir read, or small in-file
Forensics payloads), so every run is byte-stable. These tests pin the flag
derivation, the fatal cheap-flip → taste path, and the cache-once contract.

    python3 -m pytest tests/test_u7_forensics.py -v
    python3 tests/test_u7_forensics.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.forensics import (  # noqa: E402
    CONFIG, RecordedForensicsModel, canonical_model, fatal_anti_signals,
    forensics_flags, forensics_for, layout_docks,
)
from gaff_engine.schemas import (  # noqa: E402
    FlagCode, FlagKind, FlagSeverity, Forensics,
)
from gaff_engine.validate import validate  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import (  # noqa: E402
    GOLDEN_FORENSICS, GOLDEN_LISTING,
)


def _codes(flags):
    return [f.code.value for f in flags]


# ---------------------------------------------------------------------------
# 1 · The canonical De Beauvoir read reproduces §5.5's worked example + the
#     golden ScoreResult's forensics-sourced flag.
# ---------------------------------------------------------------------------

def test_canonical_read_matches_golden_forensics():
    f = forensics_for(GOLDEN_LISTING, canonical_model())
    assert f is GOLDEN_FORENSICS                          # replayed, keyed by listingKey
    assert f.aspect == "south-west (rear)"
    assert f.hmoTells is False and f.cheapFlipSignals == []
    assert validate(f) == []                              # schema-valid forensics@1


def test_de_beauvoir_flags_one_lower_ground_watch():
    f = forensics_for(GOLDEN_LISTING, canonical_model())
    flags = forensics_flags(f, GOLDEN_LISTING)
    assert _codes(flags) == ["lower_ground_light"]        # only the viewing watch flag
    fl = flags[0]
    assert fl.severity == FlagSeverity.WATCH and fl.kind == FlagKind.VIEWING
    assert fl.source == "forensics"
    assert fatal_anti_signals(f) == []                    # nothing fatal on the golden


# ---------------------------------------------------------------------------
# 2 · Cache-once per listingKey (the unit-economics contract, §5.5).
# ---------------------------------------------------------------------------

def test_cache_once_per_listing_key():
    calls = {"n": 0}

    class Counting(RecordedForensicsModel):
        def __call__(self, listing):
            calls["n"] += 1
            return super().__call__(listing)

    model = Counting({GOLDEN_LISTING.listingKey: GOLDEN_FORENSICS})
    cache = {}
    a = forensics_for(GOLDEN_LISTING, model, cache=cache)
    b = forensics_for(GOLDEN_LISTING, model, cache=cache)
    assert a is b and calls["n"] == 1                     # vision paid once
    assert len(cache) == 1


# ---------------------------------------------------------------------------
# 3 · The fatal cheap-flip — the one Forensics output that can kill (§5.5).
# ---------------------------------------------------------------------------

def test_cheap_flip_is_fatal_and_serious():
    f = Forensics(listingKey="flip1",
                  cheapFlipSignals=["grey landlord refurb", "LVT laminate", "white-box flip"])
    flags = forensics_flags(f)
    assert FlagCode.CHEAP_CARELESS_SPEC.value in _codes(flags)
    cf = [x for x in flags if x.code == FlagCode.CHEAP_CARELESS_SPEC][0]
    assert cf.severity == FlagSeverity.SERIOUS and cf.source == "forensics"
    # it feeds taste a FATAL anti-signal (small penalty; the ≤2.0 cap does the kill).
    fatal = fatal_anti_signals(f)
    assert len(fatal) == 1 and fatal[0][2] is True
    assert fatal[0][1] == CONFIG["cheapFlipTastePenalty"]


def test_cheap_flip_flag_is_first():
    """The kill leads the flag list; viewing watch-flags follow."""
    f = Forensics(listingKey="flip2", cheapFlipSignals=["laminate"],
                  aspect="north-facing", hmoTells=True, floorPosition="lower ground")
    codes = _codes(forensics_flags(f))
    assert codes[0] == "cheap_careless_spec"
    assert set(codes[1:]) == {"lower_ground_light", "north_facing", "hmo_history"}


# ---------------------------------------------------------------------------
# 4 · Viewing flags — north-facing, HMO tells.
# ---------------------------------------------------------------------------

def test_north_facing_watch_flag():
    f = Forensics(listingKey="n1", aspect="north-facing (single aspect)")
    flags = forensics_flags(f)
    assert _codes(flags) == ["north_facing"]
    assert flags[0].kind == FlagKind.VIEWING and flags[0].severity == FlagSeverity.WATCH


def test_hmo_tells_watch_flag():
    f = Forensics(listingKey="h1", hmoTells=True)
    flags = forensics_flags(f)
    assert _codes(flags) == ["hmo_history"]
    assert flags[0].kind == FlagKind.VIEWING


def test_clean_read_no_flags():
    f = Forensics(listingKey="clean", cheapFlipSignals=[], hmoTells=False,
                  aspect="south (rear)", floorPosition="first floor")
    assert forensics_flags(f) == []
    assert fatal_anti_signals(f) == []


# ---------------------------------------------------------------------------
# 5 · Layout kills — the width/separate-living docks the read surfaces.
# ---------------------------------------------------------------------------

def test_layout_docks_walk_through_and_skinny():
    f = Forensics(listingKey="lay1", walkThroughBedroom=True, roomWidthsM=[2.46, 3.1])
    notes = layout_docks(f)
    assert any("walk-through" in n for n in notes)
    assert any("2.46" in n for n in notes)               # skinny room < 2.6 m
    # a wide, well-laid-out home surfaces nothing.
    assert layout_docks(Forensics(listingKey="lay2", walkThroughBedroom=False,
                                  roomWidthsM=[3.8, 4.2])) == []


# ---------------------------------------------------------------------------
# 6 · The conservative fallback — an unknown listing is never silently killed.
# ---------------------------------------------------------------------------

def test_unknown_listing_conservative_no_kill():
    model = RecordedForensicsModel({})                   # no recordings
    listing = {"listingKey": "unseen", "id": "x",
               "description": "A bright raised ground and lower ground flat.",
               "keyFeatures": ["Lower ground kitchen"]}
    f = forensics_for(listing, model)
    assert f.cheapFlipSignals == [] and f.hmoTells is False  # never invents a kill
    # but it DOES read floor position from the listing text -> a viewing flag.
    assert "lower_ground_light" in _codes(forensics_flags(f, listing))
    assert fatal_anti_signals(f) == []


# ---------------------------------------------------------------------------
# 7 · lower_ground detection reads either the forensics floor OR the listing text.
# ---------------------------------------------------------------------------

def test_lower_ground_from_listing_text_when_forensics_silent():
    f = Forensics(listingKey="lg1")                      # no floorPosition
    listing = {"listingKey": "lg1", "keyFeatures": ["Garden level maisonette"]}
    assert "lower_ground_light" in _codes(forensics_flags(f, listing))


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
        except Exception as e:
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
