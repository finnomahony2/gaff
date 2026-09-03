"""M1 tests — the real Gaff engine (03-engine §5.0), all three scorers live.

DETERMINISTIC + OFFLINE: :func:`score` reads only the on-disk
``data/comps_enriched.json`` cache (the date-vetted HM Land Registry + EPC comps
from U9/EPC) and the golden fixtures; never the network. The excluded case is
built with :func:`dataclasses.replace` on the golden fixtures so the shared
oracle is never mutated. Reproducible on every run.

Runnable two ways (matching tests/test_u3_value.py / tests/test_u4_rules.py):

    python3 -m pytest tests/test_engine.py -v     # if pytest is installed
    python3 tests/test_engine.py                  # plain-stdlib fallback
"""

import dataclasses
import os
import sys

# Make the repo root importable whether run by pytest (from root) or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.engine import (  # noqa: E402
    ENGINE_MODE, TASTE_CALIBRATION, TASTE_READ_NOTE, score,
)
from gaff_engine.composite import composite, taste_score  # noqa: E402
from gaff_engine.value import value_score  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402
from gaff_engine.schemas import (  # noqa: E402
    Availability, ComponentName, FlagCode, FlagKind, Forensics, Gate,
    ScoreResult, TasteResult, ValueTag, ValueVerdict,
)
from gaff_engine.forensics import RecordedForensicsModel  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import (  # noqa: E402
    GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH,
)


def _golden():
    return score(GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH)


def _excluded():
    """The golden De Beauvoir subject with one bathroom, against a hard
    ``min_baths >= 2`` gate — a genuine hard-gate exclusion (no mutation of the
    shared fixtures)."""
    listing = dataclasses.replace(GOLDEN_LISTING, baths=1)
    search = dataclasses.replace(GOLDEN_SEARCH,
                                 gates=[Gate(code="min_baths", op=">=", value=2)])
    return score(listing, GOLDEN_PERSON, search)


# ---------------------------------------------------------------------------
# 1 · The golden subject is NOT excluded and the result is schema-valid.
# ---------------------------------------------------------------------------

def test_golden_not_excluded_and_schema_valid():
    r = _golden()
    assert isinstance(r, ScoreResult)
    assert r.rules.excluded is False
    assert r.rules.gatesPassed is True
    assert r.valueVerdict is not None
    assert r.taste is not None
    assert validate(r) == []                              # U1 contract-clean


# ---------------------------------------------------------------------------
# 2 · The real Value Verdict — tag FAIR, fair estimate ~£1.2m.
# ---------------------------------------------------------------------------

def test_golden_value_tag_fair_and_fair_estimate_1p2m():
    r = _golden()
    vv = r.valueVerdict
    assert isinstance(vv, ValueVerdict)
    assert vv.tag == ValueTag.FAIR                        # headline steal, honest fair
    assert 1_150_000 <= vv.fairEstimate <= 1_300_000, vv.fairEstimate   # ~£1.2m (real 1,236,000)
    # the gauge fields the M1 card fills are real + present.
    assert vv.streetMedianPerSqft and vv.streetMedianPerSqft > 0
    assert vv.band.low < vv.fairEstimate < vv.band.high
    assert 0.5 <= vv.confidence < 0.75                    # real medium confidence


# ---------------------------------------------------------------------------
# 3 · The composite is ~7.8 and recomputes from the three components (A5).
# ---------------------------------------------------------------------------

def test_golden_composite_approx_7_8_and_recomputable():
    r = _golden()
    assert abs(r.composite - 7.6) <= 0.2, r.composite     # ~7.6 (real, HPI time-adjusted)
    # A5: for a scored result, composite == mix over (taste, rules, value).
    expected = composite(r.taste.score, r.rules.score,
                         value_score(r.valueVerdict), GOLDEN_SEARCH.scorerMix)
    assert r.composite == expected


# ---------------------------------------------------------------------------
# 4 · Taste is now REAL (U6 v3 pipeline) — computed, not copied: still 8.2 across
#     all eight axes, with the text-only prior and a recomputable adjustment.
# ---------------------------------------------------------------------------

def test_taste_is_real_pipeline_computed_not_copied():
    r = _golden()
    assert isinstance(r.taste, TasteResult)
    # the canonical read reproduces the golden THROUGH the pipeline: 8.2 / prior 7.4.
    assert r.taste.score == 8.2
    assert r.taste.prior == 7.4                           # text-only ablation pass
    # all eight axes present (§5.7 rule 2), and the score recomputes from them.
    assert len(r.taste.axisBreakdown) == 8
    recomputed = taste_score(r.taste.axisBreakdown, r.taste.tasteAdjustments)
    assert recomputed == r.taste.score == 8.2
    # the +0.30 named-love is emitted as a sourced adjustment (not hidden).
    assert any(a.kind == "named_love" and a.delta == 0.3 for a in r.taste.tasteAdjustments)
    # rules is the real U4 golden score; value component recomputes from deltaPct.
    assert r.rules.score == 7.5
    assert r.valueVerdict.score == value_score(r.valueVerdict)


# ---------------------------------------------------------------------------
# 5 · Confidence report combines three REAL per-scorer scalars.
# ---------------------------------------------------------------------------

def test_confidence_combines_three_real_scorers():
    c = _golden().confidence
    assert c.rules == 0.85                                # real rules confidence (§5.8)
    assert 0.5 <= c.value < 0.75                          # real medium value confidence
    assert 0.70 <= c.taste <= 0.95                        # real taste confidence (§5.8)
    assert 0.0 < c.overall <= 1.0                         # mix-weighted overall
    # the taste driver cites the eval calibration, not a stub note.
    assert any("Spearman" in d for d in c.drivers)


# ---------------------------------------------------------------------------
# 6 · Provenance — taste reads live + is calibrated; value/rules read live.
# ---------------------------------------------------------------------------

def test_provenance_marks_live_taste_and_calibration():
    r = _golden()
    assert r.engineMode == ENGINE_MODE == "live"
    assert "stub" not in r.provenanceNote.lower()        # no longer stubbed
    assert r.provenanceNote == TASTE_READ_NOTE
    # the taste calibration (MAE 1.35 / Spearman 0.79) is carried on the result.
    assert r.tasteCalibration["mae"] == TASTE_CALIBRATION["mae"] == 1.35
    assert r.tasteCalibration["spearman"] == 0.79
    comps = {c.component: c for c in r.components}
    # the taste breakdown names the pipeline + its calibration (no STUB).
    label = comps[ComponentName.TASTE_BREAKDOWN].sources[0].label
    assert "STUB" not in label and "calibrated" in label
    # value verdict + comps table read live (READY) from HM Land Registry + EPC.
    assert comps[ComponentName.VALUE_VERDICT].availability == Availability.READY
    assert "Land Registry" in comps[ComponentName.VALUE_VERDICT].sources[0].label


def test_reasons_span_taste_value_rules_and_point_at_lease():
    r = _golden()
    scorers = {rr.scorer for rr in r.reasons}
    assert {"taste", "value", "rules"} <= scorers         # all three narrated
    # the honest headline-vs-adjusted lease line is pointed at the lease_adj
    # evidence (P1 intra-object pointer) so the card can surface it.
    assert any(rr.evidenceRefs and "lease_adj" in rr.evidenceRefs for rr in r.reasons)


# ---------------------------------------------------------------------------
# 7 · Deterministic — identical inputs yield an identical result (byte-idempotent
#     build depends on this).
# ---------------------------------------------------------------------------

def test_deterministic_same_inputs_same_id_composite_and_stamp():
    a, b = _golden(), _golden()
    assert a.id == b.id
    assert a.composite == b.composite
    assert a.scoredAt == b.scoredAt
    assert validate(a) == [] and validate(b) == []


# ---------------------------------------------------------------------------
# 8 · The exclusion contract (§5.0 step 2): a hard-gate fail forces composite 0
#     and nulls taste + valueVerdict.
# ---------------------------------------------------------------------------

def test_excluded_listing_forces_zero_and_nulls_taste_value():
    r = _excluded()
    assert r.rules.excluded is True
    assert r.rules.gatesPassed is False
    assert r.rules.score == 0.0
    assert r.composite == 0.0                             # forced, not recomputed
    assert r.valueVerdict is None                         # null on exclusion
    assert r.taste is None                                # null on exclusion
    assert validate(r) == []                              # still contract-clean
    # the failing hard gate is named in the reasons.
    assert any("bath" in rr.text.lower() for rr in r.reasons), r.reasons


def test_excluded_result_confidence_is_schema_valid():
    """An excluded result still carries a valid ConfidenceReport (all four scalars
    present; taste/value are 0 because they were never spent, §5.0)."""
    c = _excluded().confidence
    assert c.rules > 0.0                                  # the real rules confidence
    assert c.taste == 0.0 and c.value == 0.0             # no taste/value spend


# ---------------------------------------------------------------------------
# 9 · Forensics (U7, §5.5) — the vision read feeds viewing flags + the fatal
#     cheap-flip. The golden emits the lower_ground_light viewing flag; a flip
#     that clears the gates is quietly killed via taste ≤ 2.0.
# ---------------------------------------------------------------------------

def test_golden_emits_forensics_viewing_flag_and_epc_flag():
    r = _golden()
    by_code = {f.code: f for f in r.flags}
    # the forensics-sourced lower-ground viewing flag (§5.5b) is now generated.
    lg = by_code[FlagCode.LOWER_GROUND_LIGHT]
    assert lg.source == "forensics" and lg.kind == FlagKind.VIEWING
    # the listing-field EPC-below-C negotiation lever is present too.
    assert FlagCode.EPC_BELOW_C in by_code
    # the forensics payload is attached to the result, and IMAGERY reads live.
    assert r.forensics is not None and r.forensics.aspect == "south-west (rear)"
    comps = {c.component: c for c in r.components}
    assert comps[ComponentName.IMAGERY].availability == Availability.READY


def test_cheap_flip_forensics_crushes_taste_and_drops_below_show():
    """A cheap flip the vision read catches forces taste ≤ 2.0, dropping the
    composite below threshold.show (6.0) — the listing is quietly killed even
    though it clears every declared gate."""
    flip = Forensics(listingKey=GOLDEN_LISTING.listingKey,
                     cheapFlipSignals=["grey landlord refurb", "white-box flip"],
                     floorPosition="raised + lower ground", aspect="south-west (rear)")
    flip_model = RecordedForensicsModel({GOLDEN_LISTING.listingKey: flip})
    r = score(GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH, forensics_model=flip_model)
    assert r.taste.fatal is True
    assert r.taste.score <= 2.0                           # fatal cap
    assert r.composite < 6.0                              # below threshold.show
    assert r.rules.excluded is False                     # it PASSED the gates...
    assert any(f.code == FlagCode.CHEAP_CARELESS_SPEC for f in r.flags)  # ...but the flip flag fires
    assert validate(r) == []


def test_forensics_flags_deduped():
    """No duplicate (code, source) flags after the rules + forensics + listing merge."""
    r = _golden()
    keys = [(f.code, f.source) for f in r.flags]
    assert len(keys) == len(set(keys))


def test_score_survives_a_listing_with_no_sqft():
    """A2 + §5.2b: a listing with no floor area must SCORE, not crash — and with
    qualifying comps it no longer dead-ends in NEEDS_DATA: the like-for-like
    fallback prices it against whole SOLD prices (mirroring
    tests/test_u3_value.py's contract), labelled plainly, with no fabricated
    £/sqft, and the full result stays schema-valid."""
    no_sqft = dataclasses.replace(GOLDEN_LISTING, sqft=None)
    r = score(no_sqft, GOLDEN_PERSON, GOLDEN_SEARCH)          # must not raise
    vv = r.valueVerdict
    assert vv.tag != ValueTag.NEEDS_DATA                      # a real verdict now
    assert "like-for-like" in vv.basis and "no floor area" in vv.basis.lower()
    assert vv.streetMedianPerSqft is None                     # honestly absent: no £/sqft
    assert vv.fairEstimate and vv.fairEstimate > 0 and vv.deltaPct is not None
    assert isinstance(r.composite, float) and 0.0 <= r.composite <= 10.0
    assert r.composite != _golden().composite                 # a coarser value read, not the golden
    assert validate(r) == []                                  # full result still contract-valid


def test_score_no_sqft_and_no_comps_is_honest_needs_data():
    """No floor area AND no comps at all keeps the honest NEEDS_DATA empty-state
    (fail-soft, A2): value drops out of the Mix, the composite is a real
    taste+rules number, and no fabricated value figure sneaks through."""
    no_sqft = dataclasses.replace(GOLDEN_LISTING, sqft=None)
    r = score(no_sqft, GOLDEN_PERSON, GOLDEN_SEARCH, comps=[])  # must not raise
    assert r.valueVerdict.tag == ValueTag.NEEDS_DATA
    assert r.valueVerdict.deltaPct is None and r.valueVerdict.fairEstimate is None
    assert isinstance(r.composite, float) and 0.0 <= r.composite <= 10.0
    assert r.composite != _golden().composite                 # value genuinely dropped
    assert validate(r) == []                                  # full result still contract-valid


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
