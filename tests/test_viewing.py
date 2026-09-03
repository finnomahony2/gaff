"""P8 viewing tests — the checklist + the debrief re-weight (08-action.md §5.1).

The two things M10 must prove:
- **generate_checklist is sourced and correct (A1).** The golden score's viewing
  flag becomes an in-person `observe`; the serious `kind:"listing"` lease flag folds
  into the `hidden_faults` ask as the `must` item (it is NOT a viewing-only fact);
  the unverified `street_scene` axis becomes an `axis_verify`; the info EPC flag is a
  `nice` listing_flag. Must leads the tick order.
- **The debrief re-weights the Person for real (A11).** Confirming `street_scene`
  through P4 `apply_feedback` tightens its belief and nudges clarity UP; the named
  love lands; the Person is written ONLY through `apply_feedback` (the record mutates
  no Person field, and the original person object is untouched).

    python3 -m pytest tests/test_viewing.py -v
    python3 tests/test_viewing.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.engine import score  # noqa: E402
from gaff_engine.elicit import person_from_profile  # noqa: E402
from gaff_engine.taste import canonical_model  # noqa: E402
from gaff_engine.forensics import canonical_model as canonical_forensics  # noqa: E402
from gaff_engine.swipe import seed_uncertainty  # noqa: E402
from gaff_engine.viewing import (  # noqa: E402
    agent_questions, checklist_sorted, debrief, generate_checklist, prepare_viewing,
)
from gaff_engine.schemas import (  # noqa: E402
    DebriefExtracted, ItemResult, NewSignal, OverallReaction, Ref, VerdictAgreement, WouldChange,
)
from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_SEARCH  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PERSON = person_from_profile(json.load(open(os.path.join(HERE, "profile.json"))))
_SCORE = score(GOLDEN_LISTING, _PERSON, GOLDEN_SEARCH,
               taste_model=canonical_model(), forensics_model=canonical_forensics())
PURSUIT = Ref(id="pursuit_dbv", schemaVersion="pursuit@1")
PREF = Ref(id="person_finn", schemaVersion="person@1")
SCOREREF = Ref(id="score_dbv", schemaVersion="score.result@1")


def _record():
    return prepare_viewing(_SCORE, GOLDEN_LISTING, GOLDEN_SEARCH, pursuit_ref=PURSUIT,
                           person_ref=PREF, score_ref=SCOREREF, viewing_id="viewing_dbv")


def _by_source(items):
    return {it.source: it for it in items}


# ---------------------------------------------------------------------------
# §5.1a/A1 — the checklist is sourced and correct.
# ---------------------------------------------------------------------------

def test_prepared_record_validates_and_is_sourced():
    rec = _record()
    assert validate(rec) == [], validate(rec)
    assert rec.status == "prepared"
    assert all(it.source and it.evidenceRef for it in rec.checklist if it.source != "playbook")
    assert all(it.mediaRefs == [] and it.status == "pending" for it in rec.checklist)


def test_viewing_flag_becomes_an_in_person_observe():
    items = _by_source(generate_checklist(_SCORE, GOLDEN_LISTING, GOLDEN_SEARCH))
    vf = items["viewing_flag"]
    assert vf.flagCode == "lower_ground_light" and vf.kind == "observe" and vf.priority == "should"
    assert vf.evidenceRef == "flag:lower_ground_light"


def test_serious_listing_flag_folds_into_the_must_ask_not_a_viewing_fact():
    items = generate_checklist(_SCORE, GOLDEN_LISTING, GOLDEN_SEARCH)
    # the serious lease flag is a kind:"listing" flag → it must NOT be a viewing_flag (A1)
    assert not any(it.source == "viewing_flag" and it.flagCode == "short_lease" for it in items)
    hidden = next(it for it in items if it.question == "hidden_faults")
    assert hidden.priority == "must" and hidden.kind == "ask_agent" and hidden.flagCode == "short_lease"


def test_unverified_axis_and_info_flag():
    items = _by_source(generate_checklist(_SCORE, GOLDEN_LISTING, GOLDEN_SEARCH))
    assert items["axis_verify"].axis == "street_scene" and items["axis_verify"].priority == "should"
    assert items["listing_flag"].flagCode == "epc_below_c" and items["listing_flag"].priority == "nice"


def test_must_leads_the_tick_order():
    ordered = checklist_sorted(generate_checklist(_SCORE, GOLDEN_LISTING, GOLDEN_SEARCH))
    assert ordered[0].priority == "must" and ordered[0].flagCode == "short_lease"
    # priorities are non-decreasing in rank (must, then should, then nice)
    ranks = [{"must": 0, "should": 1, "nice": 2}[it.priority] for it in ordered]
    assert ranks == sorted(ranks)


# ---------------------------------------------------------------------------
# §5.1c/A11 — the debrief re-weights the Person for real.
# ---------------------------------------------------------------------------

def _extraction(record):
    ids = {it.source: it.id for it in record.checklist}
    return DebriefExtracted(
        itemResults=[
            ItemResult(localItemId=ids["viewing_flag"], status="pass", result="Bright — big skylight I hadn't clocked."),
            ItemResult(localItemId=ids["axis_verify"], status="pass", result="Street quieter than assumed — confirmed positive."),
            ItemResult(localItemId=[it.id for it in record.checklist if it.question == "hidden_faults"][0],
                       status="fail", result="89-yr lease confirmed; freeholder won't engage."),
        ],
        overallReaction=OverallReaction(text="Loved it; lease is the blocker.", ratingInferred=8.4),
        wouldChange=[WouldChange(item="lease term", class_="kill")],
        newSignals=[NewSignal(kind="named_love", value="skylit kitchen (confirmed in person)")])


def _uncertainty():
    unc = seed_uncertainty(_PERSON)
    return unc


def test_debrief_resolves_items_and_records_the_verdict():
    rec = _record()
    unc = _uncertainty()
    va = VerdictAgreement(agreedAfterViewing=True, note="'fair-because-of-lease' held exactly.")
    out, person2, unc2, receipt = debrief(rec, _extraction(rec), _PERSON, unc, _SCORE, verdict_agreement=va)
    assert validate(out) == [], validate(out)
    assert out.status == "debriefed"
    hidden = next(it for it in out.checklist if it.question == "hidden_faults")
    assert hidden.status == "fail" and "lease confirmed" in hidden.result
    assert out.verdictAgreement.agreedAfterViewing is True
    assert out.savedFromMistake is False              # they didn't walk away (lease is a lever, not a kill)


def test_debrief_sharpens_the_person_clarity_up():
    rec = _record()
    unc = _uncertainty()
    street_before = unc.axes["street_scene"].sigma
    va = VerdictAgreement(agreedAfterViewing=True, note="held")
    out, person2, unc2, receipt = debrief(rec, _extraction(rec), _PERSON, unc, _SCORE, verdict_agreement=va)
    # the confirmed axis tightened, and weighted clarity ticked UP — the retention hinge
    assert unc2.axes["street_scene"].sigma < street_before
    assert unc2.axes["street_scene"].nObs == unc.axes["street_scene"].nObs + 1
    assert receipt.clarityDelta > 0
    assert "street_scene" in (receipt.before + receipt.after) or "street" in receipt.summary.lower()
    # the named love landed on the Person
    assert "skylit kitchen (confirmed in person)" in (person2.taste.lovesNamed or [])


def test_debrief_writes_the_person_only_via_apply_feedback():
    rec = _record()
    unc = _uncertainty()
    v0 = _PERSON.profile.version
    va = VerdictAgreement(agreedAfterViewing=True, note="held")
    out, person2, unc2, receipt = debrief(rec, _extraction(rec), _PERSON, unc, _SCORE, verdict_agreement=va)
    # the original Person is untouched (apply_feedback deep-copies); person2 is bumped +1
    assert _PERSON.profile.version == v0
    assert person2.profile.version == v0 + 1
    # the viewing record carries a Ref to the Person, and mutates no Person field itself
    assert out.personRef.id == "person_finn"
    assert out.debrief.feedbackRefs[0].schemaVersion == "feedback@1"


def test_saved_from_mistake_flips_on_a_walk_away():
    rec = _record()
    unc = _uncertainty()
    va = VerdictAgreement(agreedAfterViewing=False, note="the lease killed it")
    out, *_ = debrief(rec, _extraction(rec), _PERSON, unc, _SCORE, verdict_agreement=va, walked_away=True)
    assert out.savedFromMistake is True               # a confirmed must-fail that changed the action (§5.4)


# ---------------------------------------------------------------------------
# Agent questions (outside-review brief §5 item 2) — the engine's own
# uncertainty turned into per-listing "ask the agent" questions. All listings
# below are SYNTHETIC dicts (the duck-typed shape every viewing helper reads);
# nothing here quotes portal content.
# ---------------------------------------------------------------------------

def test_agent_questions_are_sourced_and_deterministic():
    qs = agent_questions(_SCORE, GOLDEN_LISTING)
    assert qs, "the golden listing (89-yr lease) must raise at least one question"
    for q in qs:
        assert q["question"] and q["evidence"] and q["trigger"], q
        assert q["kind"] in ("ask_agent", "observe"), q
    assert qs == agent_questions(_SCORE, GOLDEN_LISTING)   # same inputs, same output


def test_short_lease_raises_the_lease_question_with_the_years_as_evidence():
    qs = agent_questions(_SCORE, GOLDEN_LISTING)
    lease = [q for q in qs if q["trigger"] == "short_lease"]
    assert len(lease) == 1
    assert "ground rent" in lease[0]["question"] and "service charge" in lease[0]["question"]
    assert "89" in lease[0]["evidence"]


def test_unknown_lease_and_unknown_tenure_each_get_their_own_question():
    unknown_years = {"mode": "buy", "tenure": {"type": "leasehold"}}
    qs = agent_questions(None, unknown_years)
    assert [q["trigger"] for q in qs] == ["lease_unknown"]
    no_tenure = {"mode": "buy", "beds": 2}
    qs = agent_questions(None, no_tenure)
    assert [q["trigger"] for q in qs] == ["tenure_unknown"]
    # a rental subject gets NO purchase-tenure questions
    rental = {"mode": "rent", "beds": 2}
    assert agent_questions(None, rental) == []


def test_sqft_basis_conflict_asks_which_measurement_standard():
    listing = {"mode": "buy", "tenure": {"type": "freehold"},
               "sqft": 1000, "epcSqft": 800}
    qs = agent_questions(None, listing)
    conflict = [q for q in qs if q["trigger"] == "sqft_basis_conflict"]
    assert len(conflict) == 1
    assert "measurement standard" in conflict[0]["question"]
    assert "1,000" in conflict[0]["evidence"] and "800" in conflict[0]["evidence"]
    # within tolerance → no conflict question
    ok = dict(listing, epcSqft=980)
    assert not [q for q in agent_questions(None, ok)
                if q["trigger"] == "sqft_basis_conflict"]


def test_claimed_works_with_an_epc_area_asks_for_signoff():
    listing = {"mode": "buy", "tenure": {"type": "freehold"},
               "sqft": 1200, "epcSqft": 1150,
               "description": "Recently extended sample home with a rear addition."}
    qs = agent_questions(None, listing)
    works = [q for q in qs if q["trigger"] == "works_vs_epc"]
    assert len(works) == 1
    assert "sign-off" in works[0]["question"]
    assert "EPC" in works[0]["evidence"]
    # no EPC-side area → nothing to challenge, no question
    no_epc = {"mode": "buy", "tenure": {"type": "freehold"}, "sqft": 1200,
              "description": "Recently extended sample home."}
    assert not [q for q in agent_questions(None, no_epc)
                if q["trigger"] == "works_vs_epc"]


def test_value_uncertainty_becomes_the_agents_burden_of_proof():
    listing = {"mode": "buy", "tenure": {"type": "freehold"}}
    llf = {"tag": "fair", "basis": "like-for-like price comparison — no floor "
                                   "area was given; comp-sufficiency gate: 3<5 "
                                   "capped to fair"}
    trigs = [q["trigger"] for q in agent_questions(None, listing, value=llf)]
    assert "llf_only" in trigs and "thin_comps" in trigs
    needs = {"tag": "needs_data", "basis": "no comparable sales nearby"}
    trigs = [q["trigger"] for q in agent_questions(None, listing, value=needs)]
    assert trigs.count("no_price_evidence") == 1
    err = {"error": "no cached comparable sales to price against"}
    qs = agent_questions(None, listing, value=err)
    assert [q["trigger"] for q in qs] == ["no_price_evidence"]
    assert "no cached comparable sales" in qs[0]["evidence"]


def test_high_weight_thin_evidence_axes_become_in_person_checks_capped_at_two():
    listing = {"mode": "rent"}      # rent: no tenure questions muddy the count
    taste = {"breakdown": [
        {"axis": "light_and_volume", "score": 8, "weight": 10, "contribution": "?"},
        {"axis": "outdoor_space", "score": 7, "weight": 9, "contribution": "thin"},
        {"axis": "street_scene", "score": 7, "weight": 8, "contribution": ""},
        {"axis": "design_finish", "score": 6, "weight": 4, "contribution": ""},
        {"axis": "character_bones", "score": 8, "weight": 8.5,
         "contribution": "period cornicing visible in three photos"},
    ]}
    qs = agent_questions(None, listing, taste=taste)
    axis_qs = [q for q in qs if q["trigger"] == "axis_low_evidence"]
    # capped at two, top weights first; the well-evidenced 8.5 axis and the
    # low-weight thin one never fire
    assert len(axis_qs) == 2
    assert "light_and_volume" in axis_qs[0]["evidence"]
    assert "outdoor_space" in axis_qs[1]["evidence"]
    assert all(q["kind"] == "observe" for q in axis_qs)


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
