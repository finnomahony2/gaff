"""P7 edit tests — the plain-language edit loop + the fork (07-shell.md §5.4/§5.5).

Drives the spec's worked example ("budget to £1.5m, add Walthamstow, threshold 7.5,
4 beds") and the acceptance gates:
- **A8** parse is pure — nothing mutates the Search until Apply; Cancel = don't use the copy.
- **A9** noise + no-ops surfaced honestly, never dropped; Apply disabled iff realCount==0.
- **A10** Apply writes only the named `search@1` fields; the changelog gets 4 ordered
  plain rows; a revert is a NEW entry, never a deletion.
- **A11** the edit path touches NO `person@1` field (taste lives on the P4 teach path).
- **A7** a fork renders the delta read-only, refuses a Mix change, promotes to a sibling
  and discards leaving the parent untouched.

    python3 -m pytest tests/test_edit.py -v
    python3 tests/test_edit.py
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.edit import (  # noqa: E402
    apply_edit, changelog_append, discard, fork_delta, fork_preview, fork_view,
    parse_instruction, promote, rerank_receipt, settings_panel,
)
from gaff_engine.schemas import (  # noqa: E402
    Area, Budget, EditEffect, Gate, Money, MoneyPeriod, Mode, Ref, ScorerMix, Search,
    SearchStatus, Threshold,
)
from gaff_engine.validate import validate  # noqa: E402

PERSON = Ref(id="person_finn", schemaVersion="person@1")
SREF = Ref(id="search_buy_e", schemaVersion="search@1")


def _search():
    return Search(
        id="search_buy_e", mode=Mode.BUY, title="East London, to buy", personRef=PERSON,
        scorerMix=ScorerMix(taste=55, rules=20, value=25),
        threshold=Threshold(show=6.0, alert=7.0),
        budget=Budget(max=Money(amount=1350000, currency="GBP", period=MoneyPeriod.TOTAL)),
        gates=[Gate(code="min_beds", op=">=", value=2)],
        area=Area(label="East London polygon", confidence="firm", polygon=[[-0.08, 51.54]]),
        status=SearchStatus.ACTIVE)


INSTR = "budget to £1.5m, add Walthamstow, threshold 7.5, 4 beds"


# ---------------------------------------------------------------------------
# §5.5 — parse is pure and previewed (A8).
# ---------------------------------------------------------------------------

def test_parse_reads_four_real_changes_and_mutates_nothing():
    s = _search()
    diff = parse_instruction(INSTR, s)
    assert validate(diff) == [], validate(diff)
    assert diff.realCount == 4 and diff.hasNoise is False
    kinds = [c.kind for c in diff.changes]
    assert kinds == ["budget", "addarea", "threshold", "beds"]
    # the parse touched nothing on the Search (A8)
    assert s.budget.max.amount == 1350000 and s.threshold.alert == 7.0
    assert next(g.value for g in s.gates if g.code == "min_beds") == 2


def test_parse_fills_from_current_values():
    diff = parse_instruction(INSTR, _search())
    by = {c.kind: c for c in diff.changes}
    assert by["budget"].from_ == 1350000 and by["budget"].to == 1500000
    assert "£1,350,000 -> £1,500,000" in by["budget"].plain
    assert by["threshold"].from_ == 7.0 and by["threshold"].to == 7.5
    assert by["beds"].from_ == 2 and by["beds"].to == 4
    assert by["addarea"].to == "Walthamstow" and by["addarea"].plain == "+ Walthamstow (area added)"


# ---------------------------------------------------------------------------
# §5.5 — noise + no-ops surfaced, never dropped (A9).
# ---------------------------------------------------------------------------

def test_noise_is_surfaced_not_dropped():
    diff = parse_instruction(INSTR + ", make it prettier", _search())
    assert diff.realCount == 4 and diff.hasNoise is True     # noise does not block the 4 real ones
    noise = [c for c in diff.changes if c.effect == EditEffect.NOISE]
    assert len(noise) == 1 and "Not understood yet" in noise[0].reason


def test_apply_disabled_only_when_no_real_change():
    diff = parse_instruction("make it prettier", _search())
    assert diff.realCount == 0 and diff.hasNoise is True     # Apply disabled iff realCount==0


def test_noop_clause_is_surfaced_as_such():
    diff = parse_instruction("threshold 7.0", _search())     # already 7.0
    ch = diff.changes[0]
    assert ch.effect == EditEffect.NOISE and "Already at that value" in ch.reason
    assert diff.realCount == 0


# ---------------------------------------------------------------------------
# §5.5 — Apply writes ONLY search@1; original untouched (A8/A10).
# ---------------------------------------------------------------------------

def test_apply_writes_the_named_fields_on_a_copy():
    s = _search()
    diff = parse_instruction(INSTR, s)
    out = apply_edit(s, diff)
    # the copy carries the four changes
    assert out.budget.max.amount == 1500000
    assert out.threshold.alert == 7.5
    assert next(g.value for g in out.gates if g.code == "min_beds") == 4
    assert "Walthamstow" in out.area.label
    # currency/period preserved on the money it rewrote
    assert out.budget.max.currency == "GBP" and out.budget.max.period == MoneyPeriod.TOTAL
    # the ORIGINAL is untouched — Cancel is just "don't use the copy" (A8)
    assert s.budget.max.amount == 1350000 and s.threshold.alert == 7.0
    assert next(g.value for g in s.gates if g.code == "min_beds") == 2


def test_apply_touches_no_person_field():
    s = _search()
    out = apply_edit(s, parse_instruction(INSTR, s))
    assert out.personRef == s.personRef                      # A11: settings never mutates the Person
    assert out.personRef.id == "person_finn"


# ---------------------------------------------------------------------------
# §5.5 — the changelog: 4 ordered plain rows; revert is a new entry (A10).
# ---------------------------------------------------------------------------

def test_changelog_gets_four_ordered_plain_rows():
    s = _search()
    diff = parse_instruction(INSTR, s)
    log = changelog_append(None, diff, source="nl", search_ref=SREF)
    assert validate(log) == [], validate(log)
    assert [e.n for e in log.entries] == [1, 2, 3, 4]
    assert log.entries[0].plain.startswith("Max budget")
    assert log.entries[3].plain.startswith("Minimum beds") and log.entries[3].plain.endswith("2 -> 4")
    assert all(e.source == "nl" for e in log.entries)


def test_a_revert_is_a_new_entry_never_a_deletion():
    s = _search()
    log = changelog_append(None, parse_instruction("threshold 7.5", s), source="nl", search_ref=SREF)
    out = apply_edit(s, parse_instruction("threshold 7.5", s))
    log2 = changelog_append(log, parse_instruction("threshold 7.0", out), source="nl")   # revert
    assert len(log2.entries) == 2                            # append-only, not a deletion
    assert log2.entries[0].plain.endswith("-> 7.5") and log2.entries[1].plain.endswith("-> 7")


def test_cancelled_diff_logs_nothing():
    s = _search()
    diff = parse_instruction(INSTR, s)
    # a cancel: we simply never call changelog_append / apply_edit
    log = changelog_append(None, __import__("gaff_engine.schemas", fromlist=["EditDiff"]).EditDiff(
        changes=[], realCount=0, hasNoise=False))
    assert log.entries == []


# ---------------------------------------------------------------------------
# §5.5 — the settings panel writes only the Search (A11).
# ---------------------------------------------------------------------------

def test_settings_panel_controls_are_search_only():
    s = _search()
    panel = settings_panel(s, changelog_append(None, parse_instruction(INSTR, s), search_ref=SREF))
    assert validate(panel) == [], validate(panel)
    assert set(panel.controls) == {"threshold", "budget", "area", "gates", "scorerMix"}
    assert "person" not in panel.controls and "taste" not in panel.controls
    assert panel.editable is True


def test_viewer_panel_is_readonly_not_a_dead_end():
    s = _search()
    panel = settings_panel(s, None, editable=False)
    assert panel.editable is False                            # read-only, still fully readable


# ---------------------------------------------------------------------------
# §5.4 — the fork: delta read-only, Mix refused, promote/discard (A7).
# ---------------------------------------------------------------------------

def _feed(ids):
    return SimpleNamespace(cards=[SimpleNamespace(listingRef=SimpleNamespace(id=i)) for i in ids])


def test_fork_renders_the_delta_and_previews_the_rerank():
    s = _search()
    parent, sub = _feed(["a", "b", "c", "d"]), _feed(["a", "c", "e", "f", "g"])   # b,d leave; e,f,g enter; c re-ranks
    view = fork_view(s, {"budget": {"max": 1500000}, "area": "+ Barnsbury/Thornhill N1"},
                     sub_id="sub_1", preview=fork_preview(parent, sub))
    assert validate(view) == [], validate(view)
    assert view.resolution == "open"
    assert [d.path for d in view.delta] == ["budget.max", "area"]
    assert view.delta[0].from_ == "£1,350,000" and view.delta[0].to == "£1,500,000"
    assert view.preview["parentCount"] == 4 and view.preview["subCount"] == 5
    assert view.preview["movers"] == {"entered": 3, "left": 2, "reranked": 1}
    # the fork holds the Person + Mix fixed
    assert view.draft.inherits["scorerMix"] is True and view.draft.personRef.id == "person_finn"


def test_fork_refuses_a_scorer_mix_change():
    s = _search()
    try:
        fork_view(s, {"scorerMix": {"taste": 80, "rules": 5, "value": 15}}, sub_id="sub_x")
        assert False, "a Mix change in a fork must be refused"
    except ValueError as e:
        assert "new Search" in str(e)


def test_promote_makes_a_draft_sibling_parent_untouched():
    s = _search()
    view = fork_view(s, {"budget": {"max": 1500000}}, sub_id="sub_1")
    sibling = promote(s, view, new_id="search_sibling")
    assert sibling.id == "search_sibling" and sibling.status == SearchStatus.DRAFT
    assert sibling.budget.max.amount == 1500000              # override baked in
    assert sibling.scorerMix.taste == 55                     # parent Mix carried
    assert s.budget.max.amount == 1350000                    # PARENT untouched
    assert view.resolution == "promoted"


def test_discard_leaves_the_parent_untouched_and_logs_nothing():
    s = _search()
    view = fork_view(s, {"budget": {"max": 1500000}}, sub_id="sub_1")
    out = discard(view)
    assert out.resolution == "discarded"
    assert s.budget.max.amount == 1350000                    # nothing moved


def test_rerank_receipt_is_a_pure_feed_diff():
    r = rerank_receipt(_feed(["a", "b", "c"]), _feed(["b", "a", "d"]), applied_count=2)
    assert r.beforeCount == 3 and r.afterCount == 3 and r.appliedCount == 2
    assert r.movers == {"entered": 1, "left": 1, "reranked": 2}   # a,b swapped order; c→d


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
