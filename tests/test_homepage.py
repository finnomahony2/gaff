"""U-homepage tests — the P7 front door + no-dead-end routing (07-shell.md §5.1-
5.2 / A1 no-dead-end). The router never yields a blank; the homepage performs a
real, honest taste-read (and says so when it can't).

    python3 -m pytest tests/test_homepage.py -v
    python3 tests/test_homepage.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.homepage import (  # noqa: E402
    NAV_MODELS, assemble_homepage, nav_model, resolve_route, taste_read,
)
from gaff_engine.swipe import seed_uncertainty  # noqa: E402
from gaff_engine.schemas import AntiSignalBelief, AxisBelief, Mode, Ref  # noqa: E402
from gaff_engine.elicit import person_from_answers  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402

MODES = ["buy", "rent", "invest", "dream"]


def _person(name="Stranger"):
    return person_from_answers({"name": name})


# ---------------------------------------------------------------------------
# No-dead-end routing (§5.1 / A1).
# ---------------------------------------------------------------------------

def test_no_hash_ever_yields_a_blank_view():
    hashes = ["#/", "", "#/bogus", "#/settings", "#/listing/nope", "#/fork/nope",
              "#/listing/L1", "#/fork/S1", "#/deals", "#/collection"]
    for mode in MODES:
        nm = nav_model(mode)
        valid = set(nm.primary) | set(nm.secondary) | {"fork", "listing", nm.home}
        for h in hashes:
            r = resolve_route(h, mode, listing_ids=("L1",), sub_ids=("S1",))
            assert r.view and r.view in valid, (mode, h, r.view)
            assert r.raw == (h or ("#/" + nm.home)) or r.view == nm.home


def test_settings_is_not_routable_and_falls_to_home():
    for mode in MODES:
        nm = nav_model(mode)
        assert resolve_route("#/settings", mode).view == nm.home     # overlay, not a route


def test_stale_deeplinks_fall_to_home_but_valid_ones_resolve():
    for mode in MODES:
        nm = nav_model(mode)
        assert resolve_route("#/listing/L1", mode, listing_ids=("L1",)).view == "listing"
        assert resolve_route("#/listing/ZZ", mode, listing_ids=("L1",)).view == nm.home
        assert resolve_route("#/fork/S1", mode, sub_ids=("S1",)).view == "fork"
        assert resolve_route("#/fork/ZZ", mode, sub_ids=("S1",)).view == nm.home
        # a real primary view for the mode resolves to itself
        assert resolve_route("#/" + nm.primary[1], mode).view == nm.primary[1]


def test_anonymous_routing_is_the_mode_less_homepage():
    assert resolve_route("#/", None).view == "homepage"
    assert resolve_route("#/anything/at/all", None).view == "homepage"   # any hash → homepage pre-auth


def test_buy_nav_model_is_authoritative():
    nm = NAV_MODELS["buy"]
    assert nm.home == "feed" and nm.primary[0] == "feed"
    assert nm.primary == ["feed", "shortlist", "map", "taste"]
    assert "game" in nm.secondary and "start" in nm.secondary
    assert nm.overlays == ["settings", "fork"]


# ---------------------------------------------------------------------------
# The homepage.spec@1 (§5.2).
# ---------------------------------------------------------------------------

def test_homepage_spec_assembles_and_validates():
    person = _person()
    unc = seed_uncertainty(person)
    refs = [Ref(id="listing_demo_englefield", schemaVersion="listing@1"),
            Ref(id="listing_demo_culford", schemaVersion="listing@1"),
            Ref(id="listing_demo_debeauvoir", schemaVersion="listing@1")]
    spec = assemble_homepage(unc, person, demo_refs=refs,
                             prediction={"score": 8.4, "line": "You'd have given this an 8.4"})
    assert validate(spec) == [], validate(spec)
    assert len(spec.frontDoors) == 4
    assert [d.mode for d in spec.frontDoors] == [Mode.BUY, Mode.RENT, Mode.INVEST, Mode.DREAM]
    assert spec.demo["cards"] == 3 and spec.demo["scoreHidden"] is True
    assert spec.provenance.isDemo is True
    assert "Everyone can see every home" in spec.headline["equation"]


# ---------------------------------------------------------------------------
# The instant taste-read — real when it can be, honest when it can't (§5.2).
# ---------------------------------------------------------------------------

def test_taste_read_composes_a_real_read():
    person = _person()
    unc = seed_uncertainty(person)
    unc.axes["light_and_volume"] = AxisBelief(mean=8.5, sigma=1.3, nObs=2)
    unc.axes["character_bones"] = AxisBelief(mean=8.6, sigma=1.3, nObs=2)
    unc.antiSignals["cheap/careless spec"] = AntiSignalBelief(
        leaning="dislike", strength=-10.0, mentions=1, confirmed=False, sigma=1.5)
    read = taste_read(unc, person, prediction={"score": 8.4, "line": "You'd have given this an 8.4"})
    assert read["sure"] is True
    assert "light and volume" in read["namedRead"] and "character" in read["namedRead"]
    assert "flip" in read["namedRead"]                       # the anti-signal, plainly
    assert "onePrediction" in read


def test_taste_read_is_honest_when_sparse():
    person = _person()
    unc = seed_uncertainty(person)                            # nothing observed yet
    read = taste_read(unc, person)
    assert read["sure"] is False
    assert "not sure yet" in read["namedRead"]               # never invents a read


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
