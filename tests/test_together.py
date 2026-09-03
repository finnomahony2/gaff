"""P8 multiplayer tests — fit-for-both, veto, invite, shortlist (08-action.md §5.5).

The core is the combination math: a home must work for EVERYONE, so `combine_fit`
uses a harmonic floor, not an average, and a veto is absolute.
- Reproduces the §5.5d worked example exactly: A 7.8 / B 6.2 → combined **6.5**,
  agreement **0.84**, B in dissent with a 1.6 gap.
- The harmonic mean punishes a low outlier (8.5/3.0 → 4.4, not the arithmetic 5.75).
- A veto forces the combined score to 0, gates the home out, and requires a reason.
- Accepting an invite writes the invitee's Person into `search.collaborators` (P1).

    python3 -m pytest tests/test_together.py -v
    python3 tests/test_together.py
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.together import (  # noqa: E402
    accept_invite, combine_fit, harmonic_mean, make_invite, make_vote, shared_shortlist, tally,
)
from gaff_engine.schemas import Ref, Role  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402

PERSON_A = Ref(id="person_a", schemaVersion="person@1")
PERSON_B = Ref(id="person_b", schemaVersion="person@1")
SREF = Ref(id="search_e8", schemaVersion="search@1")
LREF = Ref(id="listing_dbv", schemaVersion="listing@1")
PURSUIT = Ref(id="pursuit_dbv", schemaVersion="pursuit@1")


def _score(composite, reason="They like it.", flag_code="short_lease", flag_sev="serious"):
    return SimpleNamespace(
        composite=composite,
        reasons=[SimpleNamespace(polarity="+", text=reason, scorer="taste")],
        flags=[SimpleNamespace(code=flag_code, severity=flag_sev)])


def _members(a_c, b_c, *, b_veto=False):
    return [
        {"person_ref": PERSON_A, "score": _score(a_c, "Their A-tier Victorian maisonette — character, SW garden."),
         "score_ref": Ref(id="score_a", schemaVersion="score.result@1")},
        {"person_ref": PERSON_B, "score": _score(b_c, "Good light and a private garden."),
         "score_ref": Ref(id="score_b", schemaVersion="score.result@1"), "veto": b_veto,
         "veto_reason": "won't take a sub-90 lease" if b_veto else None},
    ]


# ---------------------------------------------------------------------------
# §5.5b — the fit-for-both math.
# ---------------------------------------------------------------------------

def test_reproduces_the_de_beauvoir_worked_example():
    fit = combine_fit(_members(7.8, 6.2), search_ref=SREF, listing_ref=LREF, fit_id="fit_1")
    assert validate(fit) == [], validate(fit)
    assert fit.combined["score"] == 6.5              # harmonicMean(7.8,6.2)=6.91 × 0.936 → 6.5
    assert fit.combined["agreement"] == 0.84         # 1 − 1.6/10
    assert fit.vetoed is False
    d = fit.combined["dissent"]
    assert len(d) == 1 and d[0]["personRef"].id == "person_b" and d[0]["gap"] == 1.6
    member_b = next(m for m in fit.memberFits if m.personRef.id == "person_b")
    assert member_b.composite == 6.2 and member_b.topFlagForThem == "short_lease (serious)"


def test_harmonic_mean_punishes_a_low_outlier():
    assert round(harmonic_mean([8.5, 3.0]), 1) == 4.4      # not the arithmetic 5.75
    # a home one person loves and the other hates cannot ride into the shared shortlist
    fit = combine_fit(_members(8.5, 3.0), search_ref=SREF, listing_ref=LREF, fit_id="fit_2")
    assert fit.combined["score"] < 4.5


def test_agreement_is_high_when_they_agree():
    fit = combine_fit(_members(7.5, 7.6), search_ref=SREF, listing_ref=LREF, fit_id="fit_3")
    assert fit.combined["agreement"] >= 0.98 and fit.combined["dissent"] == []


# ---------------------------------------------------------------------------
# §5.5c — the veto is absolute (and accountable).
# ---------------------------------------------------------------------------

def test_veto_forces_the_combined_to_zero_and_gates_it_out():
    fit = combine_fit(_members(7.8, 6.2, b_veto=True), search_ref=SREF, listing_ref=LREF, fit_id="fit_4")
    assert fit.vetoed is True and fit.combined["score"] == 0.0
    assert [r.id for r in fit.vetoBy] == ["person_b"]
    member_b = next(m for m in fit.memberFits if m.personRef.id == "person_b")
    assert member_b.veto is True


def test_a_veto_requires_a_reason():
    v = make_vote(PURSUIT, SREF, LREF, PERSON_B, "veto", vote_id="v1", reason="sub-90 lease")
    assert validate(v) == [], validate(v)
    try:
        make_vote(PURSUIT, SREF, LREF, PERSON_B, "veto", vote_id="v2")     # no reason
        assert False, "a veto with no reason must be rejected"
    except ValueError as e:
        assert "reason" in str(e)
    # up/down need no reason
    assert make_vote(PURSUIT, SREF, LREF, PERSON_A, "up", vote_id="v3").value == "up"


def test_tally_nets_up_and_down():
    votes = [make_vote(PURSUIT, SREF, LREF, PERSON_A, "up", vote_id="a"),
             make_vote(PURSUIT, SREF, LREF, PERSON_B, "down", vote_id="b")]
    assert tally(votes) == 0


# ---------------------------------------------------------------------------
# §5.5a — the invite lifecycle → search.collaborators.
# ---------------------------------------------------------------------------

def test_accept_invite_writes_the_person_into_collaborators():
    inv = make_invite(SREF, "editor@example.com", PERSON_A, "editor", invite_id="inv_1", message="join our E8 hunt?")
    assert validate(inv) == [], validate(inv)
    assert inv.status == "pending" and inv.personRef is None
    accepted, collab = accept_invite(inv, PERSON_B)
    assert accepted.status == "accepted" and accepted.personRef.id == "person_b"
    assert collab.role == Role.EDITOR and collab.personRef.id == "person_b"
    assert inv.status == "pending"                    # the original is untouched (accept returns a copy)


def test_invite_role_must_be_editor_or_viewer():
    try:
        make_invite(SREF, "x@y.com", PERSON_A, "owner", invite_id="inv_x")
        assert False, "owner is the inviter, not an invite role"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# §5.5c — the shared shortlist gates vetoed homes and ranks by combined.
# ---------------------------------------------------------------------------

def test_shared_shortlist_drops_vetoed_and_ranks_by_combined():
    a = combine_fit(_members(8.0, 7.6), search_ref=SREF, listing_ref=Ref(id="L_a", schemaVersion="listing@1"), fit_id="f_a")
    b = combine_fit(_members(7.0, 6.8), search_ref=SREF, listing_ref=Ref(id="L_b", schemaVersion="listing@1"), fit_id="f_b")
    c = combine_fit(_members(9.0, 8.5, b_veto=True), search_ref=SREF, listing_ref=Ref(id="L_c", schemaVersion="listing@1"), fit_id="f_c")
    order = shared_shortlist([b, a, c])
    assert [f.listingRef.id for f in order] == ["L_a", "L_b"]      # L_c vetoed → gone; a > b by combined


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
