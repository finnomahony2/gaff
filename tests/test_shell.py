"""P7 shell tests — the frame + the switcher (07-shell.md §5.0/§5.3).

The two P7 acceptance gates this module drives:
- **A1/A13 (the no-dead-end router).** Fuzz hashes through `resolve_in_search`
  against every mode's live Search — assert a live view every time, never a
  blank; stale listing/fork args fall to the mode's home.
- **A2/A5/A6 (the switcher).** One `personRef` across four Searches (the
  architecture claim), each switch lands on the new mode's home, and every demo
  Search is badged `DEMO PERSONA` (never masquerades as the user's own).

Pure — no engine, no Person mutation (the shell computes no score, §5.0).

    python3 -m pytest tests/test_shell.py -v
    python3 tests/test_shell.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.homepage import nav_model, resolve_route  # noqa: E402
from gaff_engine.shell import (  # noqa: E402
    build_switcher, resolve_in_search, shell_layout, switch_to, switcher_entry,
)
from gaff_engine.schemas import (  # noqa: E402
    Mode, Provenance, ProvenanceSource, Ref, Route, ScorerMix, Search,
    SearchStatus,
)
from gaff_engine.validate import validate  # noqa: E402

PERSON = Ref(id="person_finn", schemaVersion="person@1")


def _search(sid, mode, title, mix, *, demo=False, status=SearchStatus.ACTIVE):
    t, r, v = mix
    return Search(
        id=sid, mode=mode, title=title, personRef=PERSON,
        scorerMix=ScorerMix(taste=t, rules=r, value=v), status=status,
        provenance=Provenance(source=ProvenanceSource.DEMO if demo else ProvenanceSource.FORWARDED_ALERT_EMAIL,
                              isDemo=demo, fetchedAt="2026-07-14T08:00:00Z", freshness="fresh"))


# Finn's real switcher shape (§5.3 worked example): one Person, four Modes.
SEARCHES = [
    _search("search_buy_e", Mode.BUY, "East London, to buy", (55, 20, 25)),
    _search("search_rent_shared", Mode.RENT, "E8 home", (60, 25, 15)),
    _search("search_dream", Mode.DREAM, "The £2.9m dream", (80, 5, 15)),
    _search("search_wales_btl", Mode.INVEST, "South Wales BTL", (30, 15, 55), demo=True),
]
BY_ID = {s.id: s for s in SEARCHES}


# ---------------------------------------------------------------------------
# §5.1/A1 — the no-dead-end router, driven through the shell wrapper.
# ---------------------------------------------------------------------------

def test_router_fuzz_never_yields_a_blank_view():
    fuzz = ["#/", "", "#/bogus", "#/settings", "#/listing/nope", "#/fork/nope",
            "#/listing/L1", "#/fork/S1", "#/deals", "#/collection", "#//", "#/x/y/z/w",
            "#/LISTING/L1", "garbage", "#/taste"]
    for s in SEARCHES:
        nm = nav_model(s.mode)
        live = set(nm.primary) | set(nm.secondary) | {"listing", "fork", nm.home}
        for h in fuzz:
            r = resolve_in_search(h, s, listing_ids=("L1",), sub_ids=("S1",))
            assert r.view and r.view in live, (s.mode, h, r.view)


def test_stale_deeplinks_fall_home_valid_ones_resolve():
    for s in SEARCHES:
        nm = nav_model(s.mode)
        assert resolve_in_search("#/listing/L1", s, listing_ids=("L1",)).view == "listing"
        assert resolve_in_search("#/listing/ZZ", s, listing_ids=("L1",)).view == nm.home
        assert resolve_in_search("#/fork/S1", s, sub_ids=("S1",)).view == "fork"
        assert resolve_in_search("#/fork/ZZ", s, sub_ids=("S1",)).view == nm.home
        assert resolve_in_search("#/settings", s).view == nm.home     # overlay, not a route


# ---------------------------------------------------------------------------
# §5.0 — shell.layout@1: anonymous vs in-app.
# ---------------------------------------------------------------------------

def test_shell_layout_anonymous_has_no_active_search():
    layout = shell_layout(Route(view="homepage", raw="#/"), anonymous=True)
    assert layout.anonymous is True
    assert layout.activeSearchRef is None and layout.mode is None
    assert layout.route.view == "homepage"
    assert validate(layout) == [], validate(layout)


def test_shell_layout_in_app_carries_active_search_and_mode():
    s = BY_ID["search_buy_e"]
    route = resolve_in_search("#/feed", s)
    layout = shell_layout(route, active_search=s, viewport="rail", theme="dark")
    assert layout.anonymous is False
    assert layout.activeSearchRef.id == "search_buy_e"
    assert layout.mode == Mode.BUY
    assert layout.theme == "dark" and layout.viewport == "rail"
    assert validate(layout) == [], validate(layout)


# ---------------------------------------------------------------------------
# §5.3/A5/A6 — the switcher: one Person, many Searches; demo always badged.
# ---------------------------------------------------------------------------

def test_switcher_is_one_person_many_searches():
    sw = build_switcher(PERSON, SEARCHES, Ref(id="search_buy_e", schemaVersion="search@1"),
                        subtitles={"search_rent_shared": "with a housemate"})
    assert validate(sw) == [], validate(sw)
    assert sw.canCreate is True                                  # creating is never a dead end (§5.7)
    assert len(sw.searches) == 4
    # A5: the architecture claim — every entry reads the SAME one Person.
    assert sw.personRef.id == "person_finn"
    assert {e.mode for e in sw.searches} == {Mode.BUY, Mode.RENT, Mode.DREAM, Mode.INVEST}
    shared = next(e for e in sw.searches if e.searchRef.id == "search_rent_shared")
    assert shared.subtitle == "with a housemate" and shared.mixSummary == "60/25/15"


def test_demo_search_is_always_badged_demo():
    entries = {e.searchRef.id: e for e in build_switcher(
        PERSON, SEARCHES, Ref(id="search_buy_e", schemaVersion="search@1")).searches}
    assert entries["search_wales_btl"].badge == "DEMO PERSONA"    # A6 (hard)
    assert entries["search_buy_e"].badge == "LIVE"
    # a draft real Search reads DRAFT, not LIVE
    draft = switcher_entry(_search("s_d", Mode.BUY, "New", (55, 20, 25), status=SearchStatus.DRAFT))
    assert draft.badge == "DRAFT"
    # a demo that is also draft still flags demo (provenance wins, A6)
    both = switcher_entry(_search("s_b", Mode.BUY, "X", (55, 20, 25), demo=True, status=SearchStatus.DRAFT))
    assert both.badge == "DEMO PERSONA"


def test_mix_summary_reads_the_lens_at_a_glance():
    e = switcher_entry(BY_ID["search_wales_btl"])
    assert e.mixSummary == "30/15/55"                           # invest: value-led


# ---------------------------------------------------------------------------
# §5.3/A2 — the switch contract: always land on the new mode's home.
# ---------------------------------------------------------------------------

def test_switch_always_lands_on_new_mode_home():
    for s in SEARCHES:
        r = switch_to(s)
        assert r.view == nav_model(s.mode).home
        assert r.arg is None                                    # never carries a cross-mode arg


def test_cross_mode_arg_does_not_survive_a_switch():
    # On a Buy listing, switch to the rent Search → lands on rent home (feed), not a broken listing.
    rent = BY_ID["search_rent_shared"]
    assert switch_to(rent).view == "feed"
    # And a stale Buy listing hash resolved in the rent Search also falls to rent home.
    assert resolve_in_search("#/listing/buy_only_id", rent, listing_ids=()).view == "feed"


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
