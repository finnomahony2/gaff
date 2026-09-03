"""U6 tests — the Taste scorer (03-engine §5.1), the proven v3 model.

DETERMINISTIC: no LLM, no network. The judgement boundary is injected as a
RecordedModel (the canonical De Beauvoir recording, or small in-file reads), so
every run is byte-stable. The eval harness (U8) measures the *live* LLM
calibration separately; these tests pin the deterministic pipeline that wraps it.

Runnable two ways (matching tests/test_u3_value.py):

    python3 -m pytest tests/test_u6_taste.py -v     # if pytest is installed
    python3 tests/test_u6_taste.py                  # plain-stdlib fallback
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.taste import (  # noqa: E402
    CONFIG, AXIS_ORDER, AxisRead, RecordedModel, TasteRead, _clamp, _round1,
    canonical_deb_reads, canonical_model, taste_result,
)
from gaff_engine.schemas import TasteAxis, TasteResult  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import (  # noqa: E402
    GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SCORE_RESULT,
)


# ---------------------------------------------------------------------------
# Light builders — a person carrying the eight weights, a flat read, a listing.
# ---------------------------------------------------------------------------

WEIGHTS = {
    "light_and_volume": 10, "outdoor_space": 9, "character_bones": 8.5,
    "width_proportion_flow": 8, "street_scene": 8, "raw_size_threshold": 6,
    "design_finish": 4, "station_proximity": 0.5,
}


def make_person(loves=None, weights=None):
    return {"taste": {"weights": weights or WEIGHTS, "lovesNamed": loves or []}}


def flat_read(score=7.0, **kw):
    """A TasteRead with all eight axes at ``score`` (base == score)."""
    axes = {k: AxisRead(score, "%s @ %s" % (k, score)) for k in WEIGHTS}
    return TasteRead(axes=axes, **kw)


def one_pass_model(read):
    """A RecordedModel that returns ``read`` for both passes (no ablation)."""
    return RecordedModel({True: read, False: read})


def make_listing(**kw):
    base = {"description": "", "keyFeatures": [], "receptions": 2, "beds": 2, "sqft": 1050}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1 · The canonical De Beauvoir replay reproduces the golden THROUGH the real
#     pipeline (not by copying the fixture): score 8.2, prior 7.4, base 7.9.
# ---------------------------------------------------------------------------

def test_canonical_reproduces_golden_score_and_prior():
    tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, canonical_model())
    assert isinstance(tr, TasteResult)
    assert tr.score == GOLDEN_SCORE_RESULT.taste.score == 8.2
    assert tr.prior == GOLDEN_SCORE_RESULT.taste.prior == 7.4
    assert tr.base == 7.9                      # weighted base 426.5/54.0
    assert tr.staged is False


def test_canonical_all_eight_axes_present_in_order():
    tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, canonical_model())
    assert len(tr.axisBreakdown) == 8
    assert [a.axis for a in tr.axisBreakdown] == list(AXIS_ORDER)
    assert [a.axis for a in tr.axisBreakdown] == \
        [a.axis for a in GOLDEN_SCORE_RESULT.taste.axisBreakdown]
    # weights carried through verbatim from person.taste.weights.
    assert all(a.weight == WEIGHTS[a.axis.value] for a in tr.axisBreakdown)


def test_recompute_contract_score_equals_base_plus_deltas():
    """§5.7 rule 2: score == clamp(round(base + Σ tasteAdjustments.delta))."""
    tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, canonical_model())
    from gaff_engine.taste import _clamp, _round1
    recomputed = _round1(_clamp(tr.base + sum(a.delta for a in tr.tasteAdjustments)))
    assert recomputed == tr.score == 8.2


def test_named_love_is_three_hits_and_capped():
    tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, canonical_model())
    love = [a for a in tr.tasteAdjustments if a.kind == "named_love"]
    assert len(love) == 1
    assert love[0].delta == 0.3                # 3 hits × 0.1, under the 0.5 cap


def test_named_love_bonus_caps_at_half():
    """Six named-love hits still cap at +0.5 (namedLoveBonusCap)."""
    read = flat_read(7.0, namedLoveHits=["a", "b", "c", "d", "e", "f"])
    tr = taste_result(make_listing(), make_person(), one_pass_model(read))
    love = [a for a in tr.tasteAdjustments if a.kind == "named_love"][0]
    assert love.delta == CONFIG["namedLoveBonusCap"] == 0.5
    assert tr.score == 7.5                      # 7.0 base + 0.5 cap


# ---------------------------------------------------------------------------
# 2 · Anti-signals — non-fatal docks; a FATAL one forces taste ≤ 2.0 + flag.
# ---------------------------------------------------------------------------

def test_non_fatal_anti_signal_docks():
    read = flat_read(7.0, antiSignalHits=[("marble", -1.0, False)])
    tr = taste_result(make_listing(), make_person(), one_pass_model(read))
    assert tr.score == 6.0                      # 7.0 − 1.0
    assert tr.fatal is False
    anti = [a for a in tr.tasteAdjustments if a.kind == "anti_signal"]
    assert anti and anti[0].delta == -1.0


def test_off_grid_anti_signal_recompute_contract():
    """§5.7 rule 2 regression: an OFF-GRID penalty (carpets −0.75, straight from
    CONFIG antiSignalPenalties via the real keyword-match path) must advance the
    score by the SAME rounded delta the tasteAdjustments row emits, so the score
    recomputes exactly from the emitted rows. Before the fix the row showed −0.8
    while the score moved by −0.75, and the recompute drifted by a tenth."""
    read = flat_read(7.0)  # antiSignalHits=None → deterministic CONFIG matching
    listing = make_listing(description="fresh carpets in every bedroom")
    tr = taste_result(listing, make_person(), one_pass_model(read))
    anti = [a for a in tr.tasteAdjustments if a.kind == "anti_signal"]
    assert len(anti) == 1 and anti[0].source == "carpets"
    assert anti[0].delta == -0.8                # −0.75 rounded ONCE, half-up
    assert tr.score == 6.2                      # 7.0 − 0.8 (not round1(6.25))
    # The contract itself: score == clamp(round1(base + Σ emitted deltas)).
    recomputed = _round1(_clamp(tr.base + sum(a.delta for a in tr.tasteAdjustments)))
    assert tr.score == recomputed, (tr.score, recomputed)


def test_fatal_anti_signal_forces_ceiling():
    """A fatal cheap/careless-spec read forces taste ≤ 2.0 regardless of base."""
    read = flat_read(8.0, antiSignalHits=[("cheap/careless spec", -2.0, True)])
    tr = taste_result(make_listing(), make_person(), one_pass_model(read))
    assert tr.score <= CONFIG["fatalTasteCeiling"] == 2.0
    assert tr.fatal is True
    assert any(a.kind == "fatal_cap" for a in tr.tasteAdjustments)
    # confidence is docked for a fatal read.
    assert tr.confidence <= 0.70


# ---------------------------------------------------------------------------
# 3 · Learned-rule caps (§5.1 stage 5) — price never enters.
# ---------------------------------------------------------------------------

def test_new_build_cap_soft_ceiling():
    read = flat_read(8.5)
    tr = taste_result(make_listing(newBuild=True), make_person(), one_pass_model(read))
    assert tr.score == CONFIG["newBuildCap"] == 7.0    # capped from 8.5
    assert any("new_build_cap" in a.source for a in tr.tasteAdjustments)


def test_modernist_icon_cap():
    read = flat_read(8.0, modernistIcon=True)
    tr = taste_result(make_listing(), make_person(), one_pass_model(read))
    assert tr.score == CONFIG["modernistIconCap"] == 4.5


def test_separate_living_room_dock_open_plan_only():
    read = flat_read(7.0, openPlanOnly=True)
    tr = taste_result(make_listing(receptions=1), make_person(), one_pass_model(read))
    assert tr.score == 6.0                      # 7.0 − 1.0 open-plan-only dock
    # with two receptions the dock does NOT fire.
    tr2 = taste_result(make_listing(receptions=2), make_person(), one_pass_model(read))
    assert tr2.score == 7.0


def test_bed_count_shape_dock_beyond_five():
    read = flat_read(7.0)
    tr = taste_result(make_listing(beds=6), make_person(), one_pass_model(read))
    assert tr.score == 6.5                      # 7.0 − 0.5 wrong-shape dock


def test_price_never_enters_taste():
    """A listing with a price field scores identically to one without —
    the Taste scorer never sees price (round-1 Cassland proof)."""
    read = flat_read(7.4)
    a = taste_result(make_listing(), make_person(), one_pass_model(read))
    b = taste_result(make_listing(buy={"price": {"amount": 9999999}}),
                     make_person(), one_pass_model(read))
    assert a.score == b.score


# ---------------------------------------------------------------------------
# 4 · The image ablation (§5.1 stage 6) — text-only prior, image-informed final.
# ---------------------------------------------------------------------------

def test_prior_is_text_only_and_below_final_on_good_stock():
    """The round-2 direction: images lift good stock, so prior < score here."""
    tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, canonical_model())
    assert tr.prior is not None and tr.prior < tr.score


def test_no_prior_when_images_off():
    image, _ = canonical_deb_reads()
    tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, one_pass_model(image),
                      use_images=False)
    assert tr.prior is None                     # no text/image ablation requested


def test_staged_flag_carries_through():
    read = flat_read(7.0, staged=True)
    tr = taste_result(make_listing(), make_person(), one_pass_model(read))
    assert tr.staged is True


# ---------------------------------------------------------------------------
# 5 · The deterministic fallback — no model hits ⇒ keyword-match the listing.
# ---------------------------------------------------------------------------

def test_named_love_fallback_keyword_match():
    """With namedLoveHits=None the pipeline keyword-matches lovesNamed against
    the listing text (synonym-aware: 'skylight' → 'skylit kitchens')."""
    read = flat_read(7.0)                        # namedLoveHits defaults to None
    person = make_person(loves=["skylit kitchens", "exposed brick"])
    listing = make_listing(description="A bright flat with a skylight and exposed brick.")
    tr = taste_result(listing, person, one_pass_model(read))
    love = [a for a in tr.tasteAdjustments if a.kind == "named_love"]
    assert love and love[0].delta == 0.2        # two hits × 0.1


def test_missing_axis_raises():
    """A read missing an axis is rejected (all eight required, §5.7 rule 2)."""
    axes = {k: AxisRead(7.0, "") for k in list(WEIGHTS)[:7]}  # only 7
    read = TasteRead(axes=axes)
    try:
        taste_result(make_listing(), make_person(), one_pass_model(read))
    except ValueError as e:
        assert "missing" in str(e).lower()
    else:
        raise AssertionError("expected ValueError on a 7-axis read")


# ---------------------------------------------------------------------------
# 6 · The result is a schema-valid TasteResult (contract check).
# ---------------------------------------------------------------------------

def test_result_is_schema_valid():
    tr = taste_result(GOLDEN_LISTING, GOLDEN_PERSON, canonical_model())
    violations = validate(tr)
    assert violations == [], violations
    # reasons are present and scoped to taste.
    assert tr.reasons and all(r.scorer == "taste" for r in tr.reasons)


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest) — mirrors tests/test_u3_value.py.
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
