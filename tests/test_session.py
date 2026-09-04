"""S1 tests — the session store: the Search constructor and what persists.

    python3 tests/test_session.py
    python3 -m pytest tests/test_session.py -v     # if pytest is installed

Every test that touches disk points ``GAFF_CACHE_DIR`` at a temp directory, so a
run never reads or writes the developer's real ``~/.gaff/cache``.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import paths, rules, session  # noqa: E402
from gaff_engine.schemas import (  # noqa: E402
    Mode, MoneyPeriod, Search, SearchStatus,
)
from gaff_engine.serialize import to_jsonable  # noqa: E402


# ---------------------------------------------------------------------------
# An isolated cache for the tests that write.
# ---------------------------------------------------------------------------

class _TempCache:
    """Point GAFF_CACHE_DIR at a fresh temp dir for the duration of a block."""

    def __enter__(self):
        self._dir = tempfile.mkdtemp(prefix="gaff-session-test-")
        self._old = os.environ.get(paths.ENV_CACHE_DIR)
        os.environ[paths.ENV_CACHE_DIR] = self._dir
        return self._dir

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop(paths.ENV_CACHE_DIR, None)
        else:
            os.environ[paths.ENV_CACHE_DIR] = self._old
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


# ---------------------------------------------------------------------------
# paths.state_path — the user cache and nothing else.
# ---------------------------------------------------------------------------

def test_state_path_is_user_cache_only():
    with _TempCache() as root:
        assert paths.state_dir() == os.path.join(root, "state")
        assert paths.state_path("session.json") == os.path.join(root, "state", "session.json")
        # No shipped fallback exists for state, by construction: there is no
        # state_candidates() to fall through to.
        assert not hasattr(paths, "state_candidates")


def test_state_path_creates_nothing():
    with _TempCache() as root:
        paths.state_path("session.json")
        paths.state_dir()
        assert not os.path.exists(os.path.join(root, "state"))


# ---------------------------------------------------------------------------
# search_from_answers — the constructor five shipped modules have needed.
# ---------------------------------------------------------------------------

def test_search_from_answers_builds_a_usable_search():
    s = session.search_from_answers({
        "mode": "buy", "nation": "england", "town": "Leamington Spa",
        "budget_min": 300000, "budget_max": 450000,
        "constraints": ["min_beds>=2", "outdoor_present", "lease_years_min>=90"],
        "name": "Finn",
    })
    assert isinstance(s, Search)
    assert s.mode == Mode.BUY
    assert s.area.label == "Leamington Spa"
    assert s.area.confidence == "rough"
    assert s.budget.min.amount == 300000 and s.budget.max.amount == 450000
    assert s.budget.max.period == MoneyPeriod.TOTAL
    assert s.status == SearchStatus.ACTIVE
    assert s.personRef.schemaVersion == "person@1"
    assert [g.code for g in s.gates] == ["min_beds", "outdoor_present", "lease_years_min"]
    assert s.nation == "england"
    assert s.isDefault is False


def test_never_generates_a_polygon_gate():
    s = session.search_from_answers({"town": "LONDON", "nation": "england"})
    assert s.area.polygon is None
    assert "inside_polygon" not in [g.code for g in s.gates]
    # And asking for one by name is refused with the reason, not dropped.
    try:
        session.gate_from_constraint("inside_polygon")
    except session.UnknownConstraint as exc:
        assert "polygon" in str(exc)
    else:
        raise AssertionError("inside_polygon should be refused by name")


def test_gate_codes_are_all_resolvable_by_the_rules_layer():
    """Every code this module can emit must be a key of rules.RESOLVERS, or the
    gate is unenforceable and would read to the user as a cleared gate."""
    for code in session.CONSTRAINT_GATES:
        assert code in rules.RESOLVERS, code
    for gate in [session.gate_from_constraint("%s=%s" % (c, v)) for c, v in
                 (("min_beds", 2), ("min_baths", 1), ("min_sqft", 700),
                  ("min_receptions", 1), ("max_price", 500000),
                  ("min_price", 100000), ("lease_years_min", 90))]:
        assert gate.op in rules.OPS
    assert session.gate_from_constraint("tenure_in=freehold,leasehold").op in rules.OPS
    assert session.gate_from_constraint("outdoor_present").op in rules.OPS


def test_constraint_forms_and_coercions():
    assert session.gate_from_constraint("min_beds>=2").value == 2
    assert session.gate_from_constraint("min_beds = 2").value == 2
    assert session.gate_from_constraint("min_beds: 2").value == 2
    assert session.gate_from_constraint("min_beds 2").value == 2
    assert session.gate_from_constraint({"code": "min_beds", "value": 2}).value == 2
    assert session.gate_from_constraint("max_price<=500k").value == 500000
    assert session.gate_from_constraint("max_price=£1,350,000").value == 1350000
    assert session.gate_from_constraint("outdoor_present").value is True
    assert session.gate_from_constraint("outdoor_present=no").value is False
    assert session.gate_from_constraint("tenure_in=freehold, share_of_freehold").value == \
        ["freehold", "share_of_freehold"]


def test_a_single_constraint_need_not_be_wrapped_in_a_list():
    """F-01 calls the argument repeatable, and a repeatable argument with one
    value arrives as a scalar. Iterating a bare string character by character
    refused it as "'m' is not a constraint" — a nonsense error at the one door
    that cannot afford a refusal."""
    for one in ("min_beds>=2", {"code": "min_beds", "value": 2}, ("min_beds>=2",)):
        s = session.search_from_answers({"constraints": one})
        assert [(g.code, g.value) for g in s.gates] == [("min_beds", 2)], one


def test_numbers_carry_the_unit_word_a_person_types():
    """"two bedrooms" arrives as "2 beds". Only a bare trailing k/m multiplies:
    "500 metres" is five hundred with a unit word, not five hundred million."""
    assert session.gate_from_constraint("min_beds>=2 beds").value == 2
    assert session.gate_from_constraint("min_sqft>=1050 sqft").value == 1050
    assert session.gate_from_constraint("lease_years_min>=90 years").value == 90
    assert session.gate_from_constraint("min_sqft>=500 metres").value == 500
    assert session.gate_from_constraint("max_price<=1.35m").value == 1350000
    try:
        session.gate_from_constraint("min_beds>=two")
    except session.UnknownConstraint as exc:
        assert "expected a number" in str(exc)
    else:
        raise AssertionError("a word where a number belongs should raise")


def test_lease_gate_is_soft_and_the_rest_are_hard():
    """The golden's rationale: a short lease flags and docks, it never
    auto-excludes. Everything else the user states is a real hard gate."""
    assert session.gate_from_constraint("lease_years_min>=90").soft is True
    assert session.gate_from_constraint("min_beds>=2").soft is None
    assert session.gate_from_constraint({"code": "lease_years_min", "value": 90,
                                         "soft": False}).soft is None


def test_unknown_constraint_and_tenure_raise_by_name():
    for bad in ("garden_size>=10", "min_bedz>=2"):
        try:
            session.gate_from_constraint(bad)
        except session.UnknownConstraint as exc:
            assert "min_beds" in str(exc)          # the vocabulary is in the message
        else:
            raise AssertionError("%s should raise" % bad)
    try:
        session.gate_from_constraint("tenure_in=freehole")
    except session.UnknownConstraint as exc:
        assert "freehold" in str(exc)
    else:
        raise AssertionError("a typo'd tenure should raise, not silently never match")


def test_nation_is_validated_not_guessed():
    assert session.normalise_nation("England") == "england"
    assert session.normalise_nation("northern ireland") == "northern_ireland"
    assert session.normalise_nation("NI") == "northern_ireland"
    assert session.normalise_nation(None) is None
    for bad in ("Britain", "Eire", "Cornwall"):
        try:
            session.normalise_nation(bad)
        except ValueError as exc:
            assert "scotland" in str(exc)
        else:
            raise AssertionError("%r should not resolve to a nation" % bad)


def test_partial_answers_do_not_raise():
    """F-01's hazard: a host will call this with everything or with nothing. A
    refusal at the front door is the one refusal the plan cannot afford."""
    s = session.search_from_answers({})
    assert s.mode == Mode.BUY and s.gates == [] and s.budget is None and s.area is None
    assert session.search_from_answers({"town": "Leeds"}).budget is None
    assert session.search_from_answers({"budget_max": 400000}).area is None


def test_budget_is_not_a_gate():
    """A stated ceiling is the Value scorer's business. It becomes a hard gate
    only when the user names it as the constraint that kills."""
    s = session.search_from_answers({"budget_max": 500000})
    assert [g.code for g in s.gates] == []
    named = session.search_from_answers({"budget_max": 500000,
                                         "constraints": ["max_price<=500000"]})
    assert [g.code for g in named.gates] == ["max_price"]


def test_stretch_reproduces_the_goldens_arithmetic():
    s = session.search_from_answers({"budget_max": 1350000})
    assert s.budget.stretchMax.amount == 1417500          # +5%, as the golden
    flat = session.search_from_answers({"budget_max": 1350000, "budget_stretch_pct": 0})
    assert flat.budget.stretchMax is None


def test_rent_budget_is_per_month_not_a_total():
    s = session.search_from_answers({"mode": "rent", "budget_max": 2000})
    assert s.budget.max.period == MoneyPeriod.PCM
    assert session.search_from_answers({"budget_max": 2000}).budget.max.period == \
        MoneyPeriod.TOTAL


def test_scorer_mix_is_the_modes_default():
    assert (session.search_from_answers({"mode": "buy"}).scorerMix.taste,
            session.search_from_answers({"mode": "buy"}).scorerMix.rules,
            session.search_from_answers({"mode": "buy"}).scorerMix.value) == (55, 20, 25)
    assert session.search_from_answers({"mode": "rent"}).scorerMix.taste == 60
    assert session.search_from_answers({"mode": "invest"}).scorerMix.value == 55


def test_search_from_answers_is_deterministic():
    answers = {"mode": "buy", "nation": "wales", "town": "Cardiff",
               "budget_max": 400000, "constraints": ["min_beds>=3"]}
    a, b = session.search_from_answers(answers), session.search_from_answers(answers)
    assert a.id == b.id
    assert to_jsonable(a) == to_jsonable(b)
    # A different answer must give a different id.
    other = dict(answers, budget_max=500000)
    assert session.search_from_answers(other).id != a.id


# ---------------------------------------------------------------------------
# default_search — the no-situate fallback.
# ---------------------------------------------------------------------------

def test_default_search_excludes_nothing():
    """Honest, not lax: an empty gate list scores 8.0 and excludes nothing, so
    'no situate has been run' degrades to today's behaviour, not to an error."""
    from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING
    s = session.default_search("buy")
    assert s.gates == [] and s.budget is None and s.area is None
    assert s.isDefault is True
    rr = rules.rules_result(GOLDEN_LISTING, s)
    assert rr.excluded is False and rr.score == 8.0


def test_default_search_takes_the_mode_from_the_listing():
    class _L:
        mode = Mode.RENT
    assert session.default_search(listing=_L()).mode == Mode.RENT
    assert session.default_search().mode == Mode.BUY


# ---------------------------------------------------------------------------
# save / load — atomic, versioned, round-tripping.
# ---------------------------------------------------------------------------

def test_save_then_load_round_trips_exactly():
    from gaff_engine.elicit import person_from_answers
    with _TempCache():
        search = session.search_from_answers({
            "mode": "buy", "nation": "wales", "town": "Cardiff",
            "budget_min": 250000, "budget_max": 400000,
            "constraints": ["min_beds>=3", "lease_years_min>=90",
                            "tenure_in=freehold,share_of_freehold"],
            "name": "Finn"})
        person = person_from_answers({"name": "Finn", "minBeds": 3})
        session.save(search, person)

        back_s, back_p = session.load()
        assert to_jsonable(back_s) == to_jsonable(search)
        assert to_jsonable(back_p) == to_jsonable(person)
        # Types, not just shapes: the enums came back as enums.
        assert isinstance(back_s.mode, Mode)
        assert back_s.budget.max.period == MoneyPeriod.TOTAL
        assert back_s.gates[1].soft is True
        # nation is an attached attribute, so it rides as its own key.
        assert back_s.nation == "wales"


def test_saved_file_is_versioned_and_lands_under_state():
    with _TempCache() as root:
        session.save(session.search_from_answers({"town": "Leeds"}), None)
        path = os.path.join(root, "state", "session.json")
        assert os.path.exists(path)
        blob = json.load(open(path, encoding="utf-8"))
        assert blob["schemaVersion"] == session.SESSION_SCHEMA
        assert blob["writtenAt"].endswith("Z")
        # Not world-readable: it carries the user's budget.
        assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_load_is_empty_when_nothing_was_saved():
    with _TempCache():
        assert session.load() == (None, None)


def test_an_unreadable_session_is_ignored_with_a_note_not_a_crash():
    with _TempCache():
        # A file from a later release.
        session.save(session.search_from_answers({"town": "Leeds"}), None)
        path = session.session_file()
        blob = json.load(open(path, encoding="utf-8"))
        blob["schemaVersion"] = "gaff.session@2"
        open(path, "w", encoding="utf-8").write(json.dumps(blob))
        assert session.load() == (None, None)
        search, note = session.search_in_use()
        assert search.isDefault is True
        assert "gaff.session@2" in note["note"]

        # And plain corruption.
        open(path, "w", encoding="utf-8").write("{not json")
        assert session.load() == (None, None)
        _s, note = session.search_in_use()
        assert "not valid JSON" in note["note"]


def test_writes_are_atomic_and_leave_no_temp_files():
    with _TempCache() as root:
        for i in range(3):
            session.save(session.search_from_answers({"budget_max": 100000 * (i + 1)}), None)
        left = os.listdir(os.path.join(root, "state"))
        assert left == ["session.json"], left


def test_clear_removes_the_session():
    with _TempCache():
        session.save(session.search_from_answers({"town": "Leeds"}), None)
        assert session.clear() is True
        assert session.load() == (None, None)
        assert session.clear() is False


# ---------------------------------------------------------------------------
# Q4 — where second and third profiles live.
# ---------------------------------------------------------------------------

def test_q4_unnamed_profile_keeps_the_legacy_path():
    """The path paths.data_file already resolves ahead of the shipped demo.
    Moving it would silently orphan a profile the user has already written."""
    with _TempCache() as root:
        legacy = session.profile_path(None)
        assert legacy == os.path.join(root, "profile.json")
        os.makedirs(root, exist_ok=True)
        open(legacy, "w", encoding="utf-8").write('{"subject": "Finn", "weights": {}}')
        assert paths.data_file("profile.json") == legacy


def test_q4_named_profiles_live_under_state_with_no_shipped_fallback():
    with _TempCache() as root:
        path = session.profile_path("Alex")
        assert path == os.path.join(root, "state", "profiles", "alex.json")
        assert session.list_profiles() == []
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write('{"subject": "Alex", "calibration": {}}')
        assert session.list_profiles() == ["alex"]
        # A second person does not overwrite the first (F-10's hazard).
        assert session.profile_path("Sam") != path


def test_profile_name_cannot_escape_the_profiles_directory():
    with _TempCache() as root:
        expected_root = os.path.join(root, "state", "profiles")
        for name in ("../../evil", "a/b", "Alex & Sam"):
            path = session.profile_path(name)
            assert os.path.dirname(os.path.abspath(path)) == expected_root, name
        for bad in ("", "   ", "..", "///"):
            try:
                session.profile_path(bad)
            except ValueError:
                pass
            else:
                raise AssertionError("%r should not be a usable profile name" % bad)


def test_profile_in_use_names_whose_weights_ran():
    with _TempCache() as root:
        # Nothing of the user's: the shipped demo, said out loud.
        demo = session.profile_in_use()
        assert demo["source"] in ("shipped_demo", "missing")
        if demo["source"] == "shipped_demo":
            assert "not your weights" in demo["note"]

        # The user's own, and it shadows the demo.
        os.makedirs(root, exist_ok=True)
        open(session.profile_path(None), "w", encoding="utf-8").write(
            json.dumps({"subject": "Finn", "weights": {}, "calibration": {"round1": {}}}))
        own = session.profile_in_use()
        assert own["source"] == "user" and own["subject"] == "Finn"
        assert own["calibrated"] is True

        # Weights passed in the call beat every file.
        assert session.profile_in_use(weights={"light_and_volume": 10})["source"] == \
            "weights_argument"

        # A named profile that does not exist says so, and names what does.
        missing = session.profile_in_use(name="Alex")
        assert missing["source"] == "missing" and "Alex" in missing["note"]


# ---------------------------------------------------------------------------
# search_in_use — Q1: a payload can never use a sticky search without saying so.
# ---------------------------------------------------------------------------

def test_search_in_use_reports_which_search_it_resolved():
    with _TempCache():
        _s, note = session.search_in_use()
        assert note["source"] == "default"

        saved = session.search_from_answers({"town": "Cardiff", "nation": "wales"})
        session.save(saved, None)
        got, note = session.search_in_use()
        assert note["source"] == "session" and got.area.label == "Cardiff"
        assert note["writtenAt"]

        passed = session.search_from_answers({"town": "Leeds"})
        got, note = session.search_in_use(search=passed)
        assert note["source"] == "argument" and got.area.label == "Leeds"


# ---------------------------------------------------------------------------
# The rebuilt objects are schema-valid.
# ---------------------------------------------------------------------------

def test_rebuilt_search_validates():
    # NB: ``gaff_engine.validate`` the attribute is the re-exported FUNCTION
    # (see gaff_engine/__init__.py), so the module is imported by path.
    from gaff_engine.validate import validate as validate_obj
    from gaff_engine.fixtures.de_beauvoir import GOLDEN_SEARCH
    # The golden is the hardest case: polygon, preferences, collaborators,
    # provenance, alert policy. If it survives the round trip, a user's does.
    back = session._rebuild(Search, to_jsonable(GOLDEN_SEARCH))
    assert to_jsonable(back) == to_jsonable(GOLDEN_SEARCH)
    assert validate_obj(back) == []


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
    print("\n%s" % ("all session tests passed" if not failures
                    else "%d FAILURES" % failures))
    sys.exit(1 if failures else 0)
