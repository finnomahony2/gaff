"""U16 tests — the generative dashboard assembler (06-dashboards §5.0-§5.4).

DETERMINISTIC: pure selection over the golden score.result. The oracle is §5.3's
two worked examples (browse → 2 slots, shortlist → 9 slots) plus the five
assembly rules' linter forms. No network, no model.

    python3 -m pytest tests/test_u16_dashboard.py -v
    python3 tests/test_u16_dashboard.py
"""

import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.dashboard import (  # noqa: E402
    BUY_LIBRARY, BUY_PROFILE, assemble_dashboard, select_components,
)
from gaff_engine.engine import score  # noqa: E402
from gaff_engine.schemas import DashboardLayout, Stage  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import (  # noqa: E402
    GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH,
)

R = score(GOLDEN_LISTING, GOLDEN_PERSON, GOLDEN_SEARCH)


def _sel(stage, person=GOLDEN_PERSON, search=GOLDEN_SEARCH):
    return select_components(R, person, search, GOLDEN_LISTING, pursuit=stage)


def _codes(slots):
    return [s.component.value for s in slots]


# ---------------------------------------------------------------------------
# 1 · The two §5.3 worked examples, field-for-field.
# ---------------------------------------------------------------------------

def test_browse_feed_is_two_slots():
    """browse → [value_verdict(compact), risk_flags] — the calm feed (A4)."""
    slots = _sel("browse")
    assert _codes(slots) == ["value_verdict", "risk_flags"]
    vv, rf = slots
    assert vv.zone == "lead" and vv.tier == 3 and vv.form == "compact"
    assert rf.zone == "lead" and rf.tier == 1 and rf.form == "full"


def test_shortlist_is_nine_slots_in_order():
    """shortlist → the nine detail slots in (zoneRank, profileIndex) order."""
    slots = _sel("shortlist")
    assert _codes(slots) == [
        "value_verdict", "risk_flags", "taste_breakdown", "cost_of_ownership",
        "price_history", "comps_table", "viewing_checklist", "commute_isochrone",
        "area_report",
    ]
    # value_verdict is full for the finalist; commute stays needs_data (rule 4).
    by = {s.component.value: s for s in slots}
    assert by["value_verdict"].form == "full"
    assert by["commute_isochrone"].state == "needs_data"
    assert by["comps_table"].state == "ready"        # 3 soldComps present


def test_negotiation_dropped_by_stage_scope_at_shortlist():
    """negotiation is stageScope:{offer} — absent everywhere but the offer stage."""
    assert "negotiation" not in _codes(_sel("shortlist"))
    assert "negotiation" in _codes(_sel("offer"))


# ---------------------------------------------------------------------------
# 2 · Determinism invariant — the reproducibility guarantee (§5.3).
# ---------------------------------------------------------------------------

def test_determinism_byte_identical_slots():
    def sig(slots):
        return [(s.component.value, s.zone, s.tier, s.form, s.state, s.expansion, s.reason)
                for s in slots]
    assert sig(_sel("shortlist")) == sig(_sel("shortlist"))
    assert sig(_sel("browse")) == sig(_sel("browse"))


# ---------------------------------------------------------------------------
# 3 · The five assembly rules — linter forms (§5.1).
# ---------------------------------------------------------------------------

def test_rule1_mission_fit_lead_is_value_verdict_no_suppressed():
    slots = _sel("shortlist")
    assert slots[0].component.value == "value_verdict"        # Buy leads value_verdict
    # no suppressed Component appears (Buy suppresses none, but the check is real).
    suppressed = set(BUY_PROFILE.suppressed or [])
    assert not (set(_codes(slots)) & suppressed)


def test_rule2_sophistication_same_set_order_deeper_expansion():
    plain = _sel("shortlist")
    forensic_person = dataclasses.replace(
        GOLDEN_PERSON, values={**GOLDEN_PERSON.values, "narrationTone": "forensic"})
    foren = _sel("shortlist", person=forensic_person)
    assert _codes(plain) == _codes(foren)                     # identical set + order
    assert [s.zone for s in plain] == [s.zone for s in foren]
    # forensic expands the body tier-2/3 a plain reader leaves collapsed.
    plain_body = [s.expansion for s in plain if s.zone == "body"]
    foren_body = [s.expansion for s in foren if s.zone == "body"]
    assert set(plain_body) == {"collapsed-plus"} and set(foren_body) == {"expanded"}


def test_rule3_progressive_disclosure_browse_feed_discipline():
    """The browse feed carries no full tier-3 and no tier-3 outside lead; its only
    tier-3 is the compact lead Value Verdict — even though composite ≥ alert."""
    slots = _sel("browse")
    # De Beauvoir composite (7.7) ≥ threshold.alert (7.5): finalist is *eligible*,
    # yet the browse-feed discipline still drops every non-lead tier-2/3.
    assert R.composite >= GOLDEN_SEARCH.threshold.alert
    for s in slots:
        if s.tier == 3:
            assert s.zone == "lead" and s.form == "compact"
        assert not (s.tier >= 2 and s.zone != "lead")        # nothing heavy in the feed


def test_rule4_sourcing_every_slot_sourced_needs_data_kept():
    slots = _sel("shortlist")
    assert all(s.sources for s in slots)                     # every Slot carries sources
    # a needs_data Component stays (as a placeholder), not dropped.
    assert any(s.state == "needs_data" for s in slots)


def test_rule5_narration_bounded_and_removable():
    """Removing narration leaves the Slot[] byte-identical; the narration adds no
    claim absent from reasons/flags/valueVerdict."""
    layout = assemble_dashboard(R, GOLDEN_PERSON, GOLDEN_SEARCH, GOLDEN_LISTING, pursuit="shortlist")
    bare = _sel("shortlist")
    assert [s.component.value for s in layout.slots] == [s.component.value for s in bare]
    # the headline's numbers come from the valueVerdict (no invented figure) — the
    # magnitudes appear (the sign uses the U+2212 glyph, matching the card grammar).
    vv = R.valueVerdict
    assert ("%.1f%%" % abs(vv.headlineDeltaPct) in layout.narration.headline
            and "%.1f%%" % abs(vv.deltaPct) in layout.narration.headline)
    # the subhead is a verbatim flag text (cites, never editorialises).
    assert layout.narration.subhead in [f.text for f in R.flags]


# ---------------------------------------------------------------------------
# 4 · Stage emphasis (rule 1 / P5 step 6) — the stage leads with what it needs.
# ---------------------------------------------------------------------------

def test_stage_emphasis_viewing_and_offer():
    assert _sel("viewing")[0].component.value == "viewing_checklist"   # promoted
    assert _sel("offer")[0].component.value == "negotiation"           # promoted
    # the truth (value verdict) stays in the row right behind the promoted lead.
    assert "value_verdict" in _codes(_sel("viewing"))


# ---------------------------------------------------------------------------
# 5 · The library + the assembled layout are well-formed.
# ---------------------------------------------------------------------------

def test_library_has_the_buy_set():
    codes = {c.component.value for c in BUY_LIBRARY.components}
    need = {"value_verdict", "risk_flags", "taste_breakdown", "cost_of_ownership",
            "price_history", "comps_table", "viewing_checklist", "commute_isochrone",
            "area_report", "negotiation"}
    assert need <= codes
    # every card declares the required component.spec@1 fields.
    for c in BUY_LIBRARY.components:
        assert c.tier in (1, 2, 3) and c.typicalZone and c.whenShown and c.sophistication
        assert {"plain", "warm", "forensic"} <= set(c.sophistication)


def test_layout_is_schema_valid():
    layout = assemble_dashboard(R, GOLDEN_PERSON, GOLDEN_SEARCH, GOLDEN_LISTING, pursuit="shortlist")
    assert isinstance(layout, DashboardLayout)
    assert layout.stage == Stage.SHORTLIST
    assert layout.libraryVersion == BUY_LIBRARY.libraryVersion
    assert validate(layout) == [], validate(layout)


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
