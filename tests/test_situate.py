"""F-01 tests — the front door, and the table it answers with.

    python3 tests/test_situate.py
    python3 -m pytest tests/test_situate.py -v     # if pytest is installed

Three things are under test, in the order they can break.

**The acceptance, as the plan wrote it.** A Leeds first-time buyer, a Cardiff
renter and an Edinburgh buyer each get a truthful three-line answer before any
evidence call runs, and the Edinburgh one says no open sold-price data exists
rather than offering a warm that cannot work. That last clause is the sharpest
of the three: an offer that cannot work is worse than a plain no, because the
user spends a live call finding out.

**The hazard.** An MCP host will call ``situate`` with everything at once or
with nothing at all. A refusal at the front door is the one refusal this build
cannot afford, so nothing here may raise: unstated answers become ``unknown``
rows plus a named list, and unreadable ones are named back without being coerced
into something the user did not say.

**The extraction.** ``coverage`` and ``situate`` now read one cache walk
(``gaff_engine.cachemap``). The tests that matter are the ones that would catch
them drifting apart, because the failure mode is not a crash — it is two verbs
telling one user two different stories inside one release.

A note on the London assertions: ``data/comps`` and ``data/hpi`` are gitignored,
so a bare clone of the lab has neither. Those tests say so and stand down rather
than failing, which is the honest reading of a suite whose fixtures are half
untracked.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import cachemap, netgate, paths, session, tools  # noqa: E402


class _Cache:
    """A temp user cache, set AFTER import (tests/test_cache_hygiene.py's rule:
    a directory bound at import time is only half isolated)."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="gaff-situate-")
        self._old = os.environ.get(paths.ENV_CACHE_DIR)
        os.environ[paths.ENV_CACHE_DIR] = self.dir
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop(paths.ENV_CACHE_DIR, None)
        else:
            os.environ[paths.ENV_CACHE_DIR] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def _london_warmed():
    return bool(cachemap.comps_map().get("london"))


def _stood_down(what):
    print("     (stood down: %s — data/comps and data/hpi are gitignored)" % what)


# ---------------------------------------------------------------------------
# The acceptance: three people, three truthful answers.
# ---------------------------------------------------------------------------

def test_the_edinburgh_buyer_is_told_no_sold_prices_exist_and_offered_no_warm():
    """The sharpest clause of F-01's acceptance. Scotland has no open sold-price
    dataset, and every warm this build owns is an HM Land Registry Price Paid
    pull — so an offer here would spend a live call to learn nothing."""
    with _Cache():
        out = tools.situate(mode="buy", nation="scotland", town="Edinburgh",
                            budget_max="450k")
    assert len(out["summary"]) == 3
    comps = _row(out, "sold_comps")
    assert comps["state"] == "no"
    assert comps["actionable"] is False
    assert "Registers of Scotland" in comps["unlocked_by"]
    assert out["warms_offered"] == [], out["warms_offered"]
    # And nowhere in the payload is a warm INVOCATION printed. "No warm reaches
    # them" is the right sentence; "warm flips_town=EDINBURGH" is a live call
    # that would cost the user a request to learn nothing.
    blob = json.dumps(out["feasibility"])
    for command in ("warm street=", "warm flips_town="):
        assert command not in blob, \
            "a Scottish answer printed %r, a warm that cannot work" % command


def test_the_leeds_first_time_buyer_gets_three_true_lines_and_a_named_warm():
    with _Cache():
        out = tools.situate(mode="buy", nation="england", town="Leeds",
                            budget_max="320k", constraints=["min_beds>=2"])
    assert len(out["summary"]) == 3
    assert "LEEDS" in out["summary"][0]
    comps = _row(out, "sold_comps")
    assert (comps["state"], comps["actionable"]) == ("no", True)
    assert comps["unlocked_by"].startswith("warm street=")
    # The offer names its cost and what it sends, both read out of netgate.
    offer = out["warms_offered"][0]
    declared = netgate.verbs("warm")[0]
    assert offer["calls"] == declared["calls"]
    assert declared["sends"] in offer["what_it_sends"]
    assert declared["host"] in offer["what_it_sends"]


def test_the_cardiff_renter_is_told_where_the_rent_pool_actually_is():
    """The pool on this machine is inner London. rent_check does NOT refuse an
    out-of-area subject — below three same-bed lets in the subject's own outcode
    it widens to the whole pool — so the front door has to say so."""
    with _Cache():
        out = tools.situate(mode="rent", nation="wales", town="Cardiff",
                            budget_max=1400)
    pool = _row(out, "rent_pool")
    shape = cachemap.rent_pool_shape()
    if not shape["present"]:
        return _stood_down("no rental pool on this machine")
    assert pool["state"] == "no"
    assert "CARDIFF" in pool["why"]
    assert "widens" in pool["why"], \
        "the cohort fallback is the whole reason this row is a warning"
    assert pool["actionable"] is True and "rental_candidates.json" in pool["unlocked_by"]


# ---------------------------------------------------------------------------
# The hazard: the front door never refuses.
# ---------------------------------------------------------------------------

def test_no_summary_line_is_an_empty_sentence():
    """Found by running the ASSEMBLED tree rather than the lab: an Edinburgh
    user with no budget stated read "I cannot say what I can answer for you
    yet: ." — the missing budget was counted as a blocking need and it blocks
    nothing. Every shape a host might send, checked for the same hole."""
    shapes = [{}, {"nation": "scotland", "town": "Edinburgh"},
              {"nation": "england"}, {"town": "Leeds"},
              {"nation": "england", "town": "Leeds"},
              {"nation": "england", "town": "LONDON", "budget_max": 900000},
              {"nation": "northern_ireland", "town": "Belfast",
               "budget_max": 250000}]
    with _Cache():
        for kwargs in shapes:
            for line in tools.situate(**kwargs)["summary"]:
                assert line.strip() and not line.rstrip().endswith(": ."), \
                    "%s produced %r" % (kwargs, line)
                assert line.endswith("."), "%s produced %r" % (kwargs, line)


def test_situate_with_nothing_at_all_returns_the_table_and_what_is_needed():
    """Nothing stated: nothing may read as answerable, and what is missing is
    named. Not every row is ``unknown`` — a machine with no rental pool at all
    already knows the answer to the rent question, wherever the user turns out
    to be — so the claim under test is "nothing is answerable and I said what I
    need", not a row count."""
    with _Cache():
        out = tools.situate()
    assert out["counts"]["yes"] == 0, out["feasibility"]
    assert out["counts"]["unknown"] >= 4
    needed = {n["answer"] for n in out["still_needed"]}
    assert {"nation", "town or outcode"} <= needed
    assert out["summary"] and out["next"]
    for row in out["feasibility"]:
        if row["state"] == "unknown":
            assert row["actionable"] is True, \
                "an unknown the user could resolve must say how"


def test_unreadable_answers_are_named_back_and_never_coerced():
    """A wrong nation must not become None-and-forgotten: silence there would
    have the table claim Price Paid coverage for a country that has none."""
    with _Cache():
        out = tools.situate(nation="banana", mode="moonshot",
                            budget_max="lots", town="Hackney")
    named = {n["answer"] for n in out["not_understood"]}
    assert {"nation", "mode", "budget_max"} <= named
    assert out["you_said"]["nation"] is None
    assert out["nation"]["england_or_wales"] is None
    assert "nation" in {n["answer"] for n in out["still_needed"]}


def test_one_unreadable_constraint_costs_only_itself():
    with _Cache():
        out = tools.situate(nation="england", town="Leeds",
                            constraints=["min_bedz>=2", "min_beds>=2"])
    assert [n["given"] for n in out["not_understood"]] == ["min_bedz>=2"]
    assert out["you_said"]["constraints"] == ["min_beds>=2"]
    assert out["search"]["gates"] == ["min_beds"]


def test_a_single_constraint_may_arrive_bare_rather_than_in_a_list():
    with _Cache():
        out = tools.situate(nation="england", town="Leeds",
                            constraints="min_beds>=3")
    assert out["search"]["gates"] == ["min_beds"] and not out["not_understood"]


def test_no_invocation_of_situate_raises():
    """Every shape a host might send, including the wrong ones."""
    shapes = [{}, {"town": "Leeds"}, {"nation": "england"},
              {"nation": "banana"}, {"mode": None, "town": ""},
              {"budget_min": "x", "budget_max": "y"},
              {"constraints": [{"code": "inside_polygon"}]},
              {"outcode": "N1", "constraints": []},
              {"town": "Leeds", "nation": "northern_ireland", "name": "Ada"}]
    with _Cache():
        for kwargs in shapes:
            out = tools.situate(**kwargs)
            assert len(out["feasibility"]) == len(cachemap.EVIDENCE), kwargs


# ---------------------------------------------------------------------------
# What situate writes, and what it must not.
# ---------------------------------------------------------------------------

def test_situate_saves_a_search_that_later_calls_inherit():
    with _Cache():
        out = tools.situate(mode="buy", nation="england", town="Leeds",
                            budget_max=320000)
        assert out["session_written"] is True
        saved, person = session.load()
        assert saved is not None and saved.area.label == "Leeds"
        assert saved.nation == "england"
        _search, note = session.search_in_use()
        assert note["source"] == "session"
        assert person is not None


def test_a_bare_situate_does_not_clobber_a_saved_search():
    """Q1's sticky-search failure, arriving through the front door: a host
    calling situate() to see what the tool does must not wipe the search the
    user set up two calls ago."""
    with _Cache():
        tools.situate(mode="buy", nation="england", town="Leeds")
        out = tools.situate()
        assert out["session_written"] is False
        saved, _ = session.load()
        assert saved is not None and saved.area.label == "Leeds"


# ---------------------------------------------------------------------------
# The table's own guard rails.
# ---------------------------------------------------------------------------

_MATRIX = [(n, p) for n in (None, "england", "wales", "scotland",
                            "northern_ireland")
           for p in (None, "Leeds", "LONDON", "Leamington Spa", "N1", "Newport")]


def test_every_row_that_is_not_yes_says_what_would_change_it():
    """A "no" with nothing after it is a dead end wearing a fact's clothes."""
    for nation, place in _MATRIX:
        for row in cachemap.situation(nation=nation, place=place)["feasibility"]:
            if row["state"] == "yes":
                continue
            assert row["unlocked_by"], (nation, place, row["evidence"])
            assert row["actionable"] in (True, False), (nation, place, row)


def test_every_row_carries_all_six_evidence_types_in_order():
    for nation, place in _MATRIX:
        rows = cachemap.situation(nation=nation, place=place)["feasibility"]
        assert [r["evidence"] for r in rows] == list(cachemap.EVIDENCE)


def test_no_scottish_or_irish_row_offers_a_price_paid_warm():
    """The Edinburgh criterion, generalised: every warm this build owns is a
    Price Paid pull, so neither nation may be offered one anywhere."""
    for nation in ("scotland", "northern_ireland"):
        for place in (None, "Edinburgh", "Perth", "Belfast", "Newport"):
            s = cachemap.situation(nation=nation, place=place)
            assert s["warms_offered"] == [], (nation, place)
            for row in s["feasibility"]:
                assert "warm street=" not in (row["unlocked_by"] or "")
                assert "warm flips_town=" not in (row["unlocked_by"] or "")


def test_nation_is_asked_not_inferred_from_a_town_name():
    """Newport, Perth and Hamilton each exist in more than one UK nation. None
    of them is warmed, so none of them may settle a nation."""
    for place in ("Newport", "Perth", "Hamilton"):
        if cachemap.is_warmed(place):          # would be an evidential answer
            continue
        nat = cachemap.resolve_nation(None, place)
        assert nat["england_or_wales"] is None, place
        assert nat["source"] == "unstated"


def test_a_warmed_town_settles_england_or_wales_from_the_cache_not_the_name():
    """The one inference, and it is evidential: Price Paid holds no other
    country's sales, so a town in that cache is in England or Wales."""
    if not _london_warmed():
        return _stood_down("no london comps in either tier")
    nat = cachemap.resolve_nation(None, "LONDON")
    assert nat["england_or_wales"] is True
    assert nat["source"] == "inferred_from_cache"
    assert nat["nation"] is None, "the inference settles the datasets, not the nation"
    assert "Price Paid" in nat["note"]


def test_an_outcode_is_not_offered_as_a_uk_hpi_region():
    row = _pick(cachemap.situation(nation="england", place="N1")["feasibility"],
                "hpi")
    assert row["state"] == "unknown" and "outcode" in row["why"]


def test_a_warmed_town_can_actually_answer_something():
    if not _london_warmed():
        return _stood_down("no london comps in either tier")
    s = cachemap.situation(nation="england", place="LONDON")
    assert _pick(s["feasibility"], "sold_comps")["state"] == "yes"
    assert s["counts"]["yes"] >= 1


# ---------------------------------------------------------------------------
# The extraction: one walk, two verbs.
# ---------------------------------------------------------------------------

def test_coverage_is_the_shared_walk_plus_its_note():
    cov = tools.coverage()
    walk = cachemap.walk()
    assert cov == dict(walk, note=cov["note"])
    assert list(cov) == ["comps_towns", "flips_towns", "datasets", "note"]


def test_situate_and_coverage_agree_on_what_is_warmed():
    """The M-6 failure this module exists to prevent: the front door and the
    coverage verb disagreeing inside one release, with no way to tell which."""
    with _Cache():
        out = tools.situate(nation="england", town="LONDON")
    cov = tools.coverage()
    warmed = out["warmed"]["comps_towns"]
    for town, streets in warmed.items():
        assert cov["comps_towns"][town]["streets"] == streets, town
    assert out["warmed"]["flips_towns"] == cov["flips_towns"]


def test_tools_still_exposes_the_moved_walk_helpers():
    """_routed_comps, _resolve_pool_town and tests/test_cache_hygiene.py all
    call these by their old names."""
    assert tools._comps_towns is cachemap.comps_map
    assert tools._street_has_sales is cachemap.street_has_sales
    assert tools._empty_streets is cachemap.empty_streets


def test_the_hpi_walk_finds_the_months_on_disk():
    months = cachemap.hpi_months()
    if not months:
        return _stood_down("no hpi cache in either tier")
    for region, got in months.items():
        assert got == sorted(got) and all(len(m) == 7 for m in got), region


# ---------------------------------------------------------------------------
# The tool surface.
# ---------------------------------------------------------------------------

def test_situate_is_declared_and_is_the_first_tool_a_host_sees():
    assert tools.TOOLS[0]["name"] == "situate", \
        "situate is the front door; a host reading tools/list top-down meets it first"
    assert "situate" in tools.DISPATCH
    schema = tools.TOOLS[0]["inputSchema"]
    assert schema["required"] == [], "the front door requires nothing"
    assert set(schema["properties"]) == {
        "mode", "nation", "town", "outcode", "budget_min", "budget_max",
        "constraints", "name"}


def test_the_budget_arguments_read_money_the_way_a_person_types_it():
    coerce = tools.COERCIONS["situate"]["budget_max"]
    assert coerce("£320,000") == 320000
    assert coerce("320k") == 320000
    assert coerce("1.35m") == 1350000


def test_situate_is_not_a_network_verb():
    """Measured properly by tests/test_netgate.py; asserted here so the
    declaration is checked beside the tool it describes."""
    assert not netgate.declares("situate")


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _pick(rows, evidence):
    return next(r for r in rows if r["evidence"] == evidence)


def _row(out, evidence):
    return _pick(out["feasibility"], evidence)


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest) — mirrors tests/test_engine.py.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception as exc:                        # noqa: BLE001
                failures += 1
                import traceback
                traceback.print_exc()
                print("FAIL %s: %s" % (name, exc))
    print("\n%s" % ("the front door holds" if not failures
                    else "%d FAILURES" % failures))
    sys.exit(1 if failures else 0)
