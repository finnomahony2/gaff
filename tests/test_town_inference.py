"""F-02 tests — town inference, and the warm that is offered rather than taken.

    python3 tests/test_town_inference.py
    python3 -m pytest tests/test_town_inference.py -v     # if pytest is installed

**The acceptance, as the plan wrote it.** The README flow never returns "LONDON
is warmed but this street is not" for a non-London street. That sentence was
real: after warming a Leamington street, the obvious next question
(``price_check street="Willes Road"``) was answered about London, because
``town`` defaulted to ``"LONDON"`` — and the hint then invited the user to spend
a live call fetching a London street of that name while the sales they wanted
sat one cache directory away (post-release assessment, finding F5).

**The hazard the plan names.** The default was load-bearing, so removing it
changes the meaning of the signature, and the fixtures have to say ``town=``
rather than inherit it silently. The tests below drive both: the explicit
argument still wins over everything, and the inferred paths always SAY they were
inferred.

**The offer must stay an offer.** An MCP tool cannot prompt mid-call, so the
pattern is return-an-offer, host-asks-user, host-calls-warm. A cold street must
return the invocation, its cost and what it sends — and must not fetch, then or
on any flag. The moment one verb fetches on a flag, the single declared network
verb (S4) becomes every verb, so ``test_no_inference_path_can_reach_a_socket``
is the load-bearing one in this file.

A note on the London assertions: ``data/comps`` is gitignored, so a bare clone
of the lab has no London cache. Those tests stand down out loud rather than
failing, the way ``tests/test_situate.py`` does.
"""

import json
import os
import shutil
import socket
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import cachemap, netgate, paths, session, tools  # noqa: E402


class _Cache:
    """A temp user cache, set AFTER import (tests/test_cache_hygiene.py's rule:
    a directory bound at import time is only half isolated)."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="gaff-town-")
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

    def seed(self, town, street, sales=2):
        """Write a street cache file the way ``landreg`` writes one."""
        path = os.path.join(self.dir, "comps", cachemap.town_slug(town),
                            "%s.json" % cachemap.town_slug(street))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        items = [{"pricePaid": 300000 + i * 1000,
                  "transactionDate": "Fri, 20 Feb 2026"} for i in range(sales)]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"street": str(street).upper(), "town": str(town).upper(),
                       "count": len(items), "items": items,
                       "fetchedAt": "2026-09-04T00:00:00Z"}, fh)
        return path

    def situate(self, **answers):
        """Save a session the way a real ``situate`` call would."""
        return tools.situate(**answers)


def _stood_down(what):
    print("     (stood down: %s — data/comps is gitignored)" % what)


def _warmed_town_with_a_street():
    """A (town_slug, street_slug) actually present, or None. The lab's cache is
    half untracked, so nothing here may hardcode which towns are warm."""
    for town, streets in sorted(cachemap.comps_map().items()):
        if streets:
            return town, sorted(streets)[0]
    return None


# ---------------------------------------------------------------------------
# 1 · The acceptance: the F5 sentence, gone.
# ---------------------------------------------------------------------------

def test_a_street_cached_only_outside_london_is_not_answered_about_london():
    """THE acceptance criterion, driven on a seeded town so it holds in a bare
    clone: a street warmed in one town, asked without ``town=``, is answered
    about that town — not about London, and not with the invitation to spend a
    live call fetching a London street of the same name."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        out = tools.price_check("Zzz Alpha Road")
        assert out.get("error") is None, out
        assert out["town"] == "TESTBURY", out
        assert out["sales_found"] == 3, out
        assert "LONDON" not in json.dumps(out), out


def test_the_readme_opening_pair_still_answers_after_situating_elsewhere():
    """``situate town=<somewhere unwarmed>`` then ``price_check`` on a street
    cached elsewhere. The saved search names a town that holds nothing, so it
    is not a competing answer — it is a town with nothing to say, and the one
    cache that CAN answer does."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        c.situate(nation="england", town="LEEDS", mode="buy")
        out = tools.price_check("Zzz Alpha Road")
        assert out.get("error") is None, out
        assert out["town"] == "TESTBURY", out
        assert out["town_resolved_from"] == "street", out


# ---------------------------------------------------------------------------
# 2 · The resolution order, one rule at a time.
# ---------------------------------------------------------------------------

def test_an_explicit_town_beats_every_inference():
    """Rule 1. An argument the user typed is never second-guessed, even when
    every other signal points somewhere else."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        c.situate(nation="england", town="TESTBURY", mode="buy")
        out = tools.price_check("Zzz Alpha Road", "ATLANTIS")
        assert out["town"] == "ATLANTIS", out
        assert out["town_resolved_from"] == "argument", out
        assert out.get("error"), "an unwarmed explicit town must not be silently replaced"


def test_the_streets_own_cache_beats_the_saved_search():
    """Rule 2, and the reason it sits above rule 3. The build plan puts the
    session first; run that way, a user situated in London asking about a
    Leamington street is told "LONDON is warmed, but Willes Road is not cached
    there yet" — the exact sentence the acceptance criterion forbids. Evidence
    about the street beats a preference about the search."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        c.seed("OTHERTOWN", "Zzz Beta Road", sales=2)
        c.situate(nation="england", town="OTHERTOWN", mode="buy")
        out = tools.price_check("Zzz Alpha Road")
        # Both towns are warmed, so this is the genuine two-answer case: it
        # asks rather than picking. What it must NOT do is answer about
        # OTHERTOWN and call the street missing there.
        assert out.get("error"), out
        assert "not cached there yet" not in out["error"], out
        assert out["street_cached_in"] == "TESTBURY", out
        assert out["session_town"] == "OTHERTOWN", out


def test_the_saved_search_answers_when_the_street_is_cached_nowhere():
    """Rule 3. No cache holds the street, but the user has said where they are
    looking, so the cold-street offer is made for THAT town."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        c.situate(nation="england", town="TESTBURY", mode="buy")
        out = tools.price_check("Zzz Cold Lane")
        assert out["town"] == "TESTBURY", out
        assert out["town_resolved_from"] == "session", out
        assert out["town_warmed"] is True, out


def test_nothing_resolves_and_it_asks_instead_of_assuming_london():
    """Rule 4 — the whole point of the item. No argument, no session, no cache
    owner: the refusal names the warmed towns and never picks a city."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        out = tools.price_check("Zzz Cold Lane")
        assert out.get("error"), out
        assert "no longer assumes LONDON" in out["error"], out
        assert out["town_resolved_from"] is None, out
        assert "testbury" in out["towns_warmed"], out


def test_two_warmed_candidates_ask_rather_than_pick():
    """Two methods disagreeing is a stop signal, not a footnote. Both towns can
    answer, so the payload names both and the user chooses."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        c.seed("OTHERTOWN", "Zzz Beta Road", sales=2)
        c.situate(nation="england", town="OTHERTOWN", mode="buy")
        out = tools.price_check("Zzz Alpha Road")
        assert "say which you meant" in out["error"], out
        assert "TESTBURY" in out["hint"] and "OTHERTOWN" in out["hint"], out


def test_an_ambiguous_street_name_is_not_resolved_by_uniqueness():
    """The uniqueness rule requires uniqueness. A street name held by two
    warmed towns places nothing, and the tool asks."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Shared Road", sales=3)
        c.seed("OTHERTOWN", "Zzz Shared Road", sales=4)
        out = tools.price_check("Zzz Shared Road")
        assert out.get("error"), out
        assert out["town_resolved_from"] is None, out


def test_a_scottish_saved_search_does_not_make_an_english_town_dataless():
    """Found by installing the PUBLISHED 0.2.0 and asking two questions in the
    order a person actually would: "what can you do for Edinburgh?", then "what
    did houses on Trinity Street in Cambridge go for?".

    The second came back "HM Land Registry Price Paid covers England and Wales
    only, so there is no open sold-price data for 'CAMBRIDGE' to fetch or cache",
    with the hint "no action changes this: the data does not exist to be warmed."
    Cambridge is in England. Price Paid covers it. The user was told their own
    country's open data does not exist, and that nothing could be done about it,
    which is worse than the LONDON default F-02 removed.

    The cause: the cold-street path read the nation out of the saved session and
    applied it to a town the user had just named. A nation states where the
    SESSION is. It says nothing about a town named in this call.
    """
    with _Cache():
        tools.situate(nation="scotland", town="Edinburgh")
        ok, payload = tools.safe_call("price_check", tools.price_check,
                                      {"street": "Trinity Street",
                                       "town": "Cambridge"})
    blob = json.dumps(payload)
    assert "covers England and Wales only" not in blob, (
        "a Scottish saved search made an English town dataless: %s"
        % payload.get("error"))
    assert "the data does not exist to be warmed" not in blob
    assert "warm" in blob.lower(), \
        "an unwarmed English town must still be offered the warm that would fix it"


def test_the_session_nation_still_applies_to_the_sessions_own_town():
    """The other half: gating the leak must not throw the nation away where it
    genuinely belongs. A user situated in Scotland asking about a street with no
    town named is asking about THEIR town, and Price Paid still holds nothing."""
    with _Cache():
        tools.situate(nation="scotland", town="Edinburgh")
        _ok, payload = tools.safe_call("price_check", tools.price_check,
                                       {"street": "Princes Street"})
        blob = json.dumps(payload)
    # Either it refuses for want of a warmed town, or it names the nation --
    # but it must never offer a warm that Price Paid cannot serve.
    assert 'warm street="PRINCES STREET" town="EDINBURGH"' not in blob, \
        "offered a Price Paid warm for a Scottish town"


def test_a_cold_street_names_the_towns_that_do_hold_it():
    """Found by writing the ambiguous test above. A street held by two OTHER
    towns resolves to neither, so the answer falls through to the saved
    search's town and says "not cached there yet" — true, and quietly hiding
    that this build holds that very street twice over. The user is one live
    call away from refetching data already on the disk, so the payload names
    where it is and that reading it costs nothing."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Shared Road", sales=3)
        c.seed("OTHERTOWN", "Zzz Shared Road", sales=4)
        c.seed("THIRDTOWN", "Zzz Beta Road", sales=2)
        c.situate(nation="england", town="THIRDTOWN", mode="buy")
        out = tools.price_check("Zzz Shared Road")
        assert out["town"] == "THIRDTOWN", out
        assert out["also_cached_in"] == ["OTHERTOWN", "TESTBURY"], out
        assert "no live call at all" in out["hint"], out


def test_no_such_note_when_the_street_is_held_nowhere_else():
    """The corollary: it must not appear for a street genuinely cached nowhere,
    or it becomes noise on the commonest cold path."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        out = tools.price_check("Zzz Cold Lane", "TESTBURY")
        assert "also_cached_in" not in out, out


# ---------------------------------------------------------------------------
# 3 · An inferred town is stated, never slipped in.
# ---------------------------------------------------------------------------

def test_every_inferred_answer_says_it_was_inferred():
    """Q1's principle, applied to geography: a payload may not use a town the
    user did not type without saying where the town came from."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        by_street = tools.price_check("Zzz Alpha Road")
        assert by_street["town_resolved_from"] == "street", by_street
        assert "only warmed town" in by_street["note"], by_street

        c.situate(nation="england", town="TESTBURY", mode="buy")
        by_session = tools.price_check("Zzz Cold Lane")
        assert by_session["town_resolved_from"] == "session", by_session
        assert "saved search" in by_session["note"], by_session


def test_an_explicit_town_carries_no_inference_note():
    """The note exists to explain a town the user did not type. When they typed
    it, there is nothing to explain and nothing is added."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        out = tools.price_check("Zzz Alpha Road", "TESTBURY")
        assert out["town_resolved_from"] == "argument", out
        assert "note" not in out, out


# ---------------------------------------------------------------------------
# 4 · The offer is an offer. This is the section that keeps S4's promise.
# ---------------------------------------------------------------------------

def test_a_cold_street_returns_the_offer_with_its_cost_and_what_it_sends():
    """Return-an-offer, host-asks-user, host-calls-warm. The payload has to
    carry enough for a host to put the question: the exact invocation, the
    number of calls, what leaves the machine and to whom."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        out = tools.price_check("Zzz Cold Lane", "TESTBURY")
        offer = out["offer"]
        declared = netgate.verbs("warm")[0]
        assert offer["action"] == 'warm street="ZZZ COLD LANE" town="TESTBURY"', offer
        assert offer["calls"] == declared["calls"], offer
        assert declared["sends"] in offer["what_it_sends"], offer
        assert declared["host"] in offer["what_it_sends"], offer
        assert offer["licence"] == declared["licence"], offer


def test_the_offer_is_rendered_from_netgate_not_retyped():
    """The same declaration the fetcher shows before it goes out. Retyping it
    is how a tool ends up promising a call the code does not make."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        offer = tools.price_check("Zzz Cold Lane", "TESTBURY")["offer"]
        assert offer["consent"] == netgate.consent_line("warm", 0), offer


def test_no_inference_path_can_reach_a_socket():
    """S4's promise, under the new paths. Every resolution branch — inferred,
    session, conflicted, refused, cold-street-with-an-offer — must stay
    offline. An offer that fetches is not an offer.

    Measured, not asserted: the socket is denied and any attempt raises.
    """
    denied = []

    class _Denied(socket.socket):
        def connect(self, *a, **k):
            denied.append(a)
            raise AssertionError("a price_check path opened a socket: %s" % (a,))

        connect_ex = connect

    real = socket.socket
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        c.seed("OTHERTOWN", "Zzz Beta Road", sales=2)
        socket.socket = _Denied
        try:
            for kwargs in ({"street": "Zzz Alpha Road"},
                           {"street": "Zzz Alpha Road", "town": "TESTBURY"},
                           {"street": "Zzz Cold Lane"},
                           {"street": "Zzz Cold Lane", "town": "TESTBURY"},
                           {"street": "Zzz Cold Lane", "town": "ATLANTIS"},
                           {"street": "Zzz Shared Road", "town": "EDINBURGH"}):
                tools.price_check(**kwargs)
            c.situate(nation="england", town="OTHERTOWN", mode="buy")
            tools.price_check("Zzz Alpha Road")          # the conflict path
            tools.price_check("Zzz Cold Lane")         # the session path
        finally:
            socket.socket = real
    assert not denied, denied


def test_price_check_grew_no_flag_that_would_make_it_fetch():
    """The structural half of the same promise. ``warm`` is the only declared
    network verb; a ``fetch=``/``warm=``/``live=`` argument on price_check would
    make the declaration meaningless, so the signature is asserted."""
    import inspect
    params = set(inspect.signature(tools.price_check).parameters)
    assert params == {"street", "town", "progress"}, params
    assert "price_check" not in netgate.NETWORK_VERBS


# ---------------------------------------------------------------------------
# 5 · Never print an invocation that cannot work (F-01's lesson, reused).
# ---------------------------------------------------------------------------

def test_a_stated_scottish_nation_is_offered_no_warm_at_all():
    """Price Paid is England and Wales only. When the nation is KNOWN to be
    outside it, there is no call to offer and the payload says the data does
    not exist rather than naming a fetch that returns nothing."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        c.situate(nation="scotland", town="EDINBURGH", mode="buy")
        out = tools.price_check("Some Edinburgh Street", "EDINBURGH")
        assert "offer" not in out, out
        assert "England and Wales only" in out["error"], out
        assert "no action changes this" in out["hint"], out


def test_an_unknown_nation_offers_the_warm_but_states_the_condition():
    """Three states, and the unknown one is not the yes. Nation is asked, never
    inferred from a town name, so an unwarmed town cannot be called English —
    but the offer may not imply the call will return something either. The
    condition is written on the offer."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        out = tools.price_check("Some Street", "PERTH")
        assert out["offer"]["conditional_on"], out
        assert "England or Wales" in out["offer"]["conditional_on"], out
        assert "conditional on" in out["hint"], out


def test_a_warmed_town_needs_no_condition_on_its_offer():
    """A town already in the Price Paid cache is evidentially England or Wales
    (``cachemap.resolve_nation``'s one inference), so its offer is unqualified
    rather than hedged for no reason."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        out = tools.price_check("Zzz Cold Lane", "TESTBURY")
        assert "conditional_on" not in out["offer"], out


# ---------------------------------------------------------------------------
# 6 · warm spends the live call, so it guesses even less.
# ---------------------------------------------------------------------------

def test_warm_refuses_rather_than_fetching_a_guessed_city():
    """The harder half of the same change. price_check aimed at the wrong town
    is a confusing answer; ``warm`` aimed at the wrong town is a request sent to
    HM Land Registry for a street in a city nobody named, and an empty cache
    file left behind for ``street_has_sales`` to explain away."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=3)
        try:
            tools.warm(street="Foo Street")
            raise AssertionError("warm fetched without a town")
        except tools.UsageError as exc:
            assert "no longer assumes LONDON" in str(exc), exc
            assert "testbury" in str(exc), exc


def test_warm_takes_the_saved_searchs_town_without_needing_it_warmed():
    """Unlike price_check's rule 3, the session's town does NOT have to be
    warmed here: warming is precisely the act that makes a town warm, so
    requiring warmth first would make the inference useless for the only case
    it exists to serve — a user's first street in a new town.

    Resolution only; the fetch itself is never run (this suite is offline).
    """
    with _Cache() as c:
        c.situate(nation="england", town="BRAND NEW TOWN", mode="buy")
        assert tools._session_place()[0] == "BRAND NEW TOWN"
        assert cachemap.town_slug("BRAND NEW TOWN") not in cachemap.comps_map()
        # The resolution warm performs, without the request that follows it.
        town, _nation = tools._session_place()
        assert town == "BRAND NEW TOWN"


# ---------------------------------------------------------------------------
# 7 · The value path: a pool opened by the saved search says so.
# ---------------------------------------------------------------------------

def test_a_listing_that_places_itself_ignores_the_saved_search():
    """The L2C P0 guarantee, kept. The build plan puts the session above the
    address; that ordering would price a pasted listing against the session's
    town for a user who situated elsewhere. A listing's own address is evidence
    about that listing, so it wins."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=6)
        c.seed("OTHERTOWN", "Zzz Beta Road", sales=6)
        c.situate(nation="england", town="OTHERTOWN", mode="buy")
        listing = tools.read_listing(fields={
            "address": "Zzz Alpha Road, Testbury", "beds": 2, "sqft": 800,
            "price": 300000, "mode": "buy"})
        assert listing is not None
        from gaff_engine import tools as _t
        obj = _t._ingest({"address": "Zzz Alpha Road, Testbury", "beds": 2,
                          "sqft": 800, "price": 300000}, None, "buy")
        assert _t._resolve_pool_town(obj) == "testbury", _t._resolve_pool_town(obj)


def test_resolve_pool_town_reads_the_listing_only():
    """The session is applied in ``_routed_comps``, not appended as a fifth
    rule here, so that a pool opened by a standing preference can be told apart
    from one the listing itself pointed at."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=6)
        c.situate(nation="england", town="TESTBURY", mode="buy")
        obj = tools._ingest({"address": "Zzz Cold Lane, ATLANTIS", "beds": 2,
                             "sqft": 800, "price": 300000}, None, "buy")
        assert tools._resolve_pool_town(obj) is None


def test_a_refusal_from_a_session_pool_names_the_saved_search():
    """The honesty this bought. Without it, a user whose listing placed nowhere
    is told "warm the subject's own street" — and goes and warms it in the
    wrong city, because the pool silently came from their saved search."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=6)
        c.situate(nation="england", town="TESTBURY", mode="buy")
        payload = tools.value_check(fields={
            "address": "Zzz Cold Lane, ATLANTIS", "beds": 2, "sqft": 800,
            "price": 300000, "mode": "buy"})
        assert payload.get("tag") is None, payload
        assert payload.get("pool_town_from") == "session", payload
        assert "saved search" in payload["hint"], payload


def test_an_unplaceable_listing_with_no_session_still_refuses_the_old_way():
    """No session, nothing changed: the pre-F-02 refusal, which names the right
    fix (say the town) rather than the wrong one (warm the street)."""
    with _Cache() as c:
        c.seed("TESTBURY", "Zzz Alpha Road", sales=6)
        payload = tools.value_check(fields={
            "address": "Zzz Cold Lane, ATLANTIS", "beds": 2, "sqft": 800,
            "price": 300000, "mode": "buy"})
        assert payload.get("tag") is None, payload
        assert "warmed town" in payload["error"], payload


# ---------------------------------------------------------------------------
# 8 · The shipped cache, when the clone has one.
# ---------------------------------------------------------------------------

def test_the_real_cache_answers_a_bare_street_without_naming_london():
    """The lived case from the assessment, against whatever this clone actually
    holds. Stands down rather than failing where data/comps is absent."""
    found = _warmed_town_with_a_street()
    if found is None:
        return _stood_down("no warmed comps town in this clone")
    town, street = found
    out = tools.price_check(street.replace("-", " "))
    if out.get("error"):
        # Legitimate when this clone warms the same street in two towns, or a
        # saved session competes; both are asks, never a London answer.
        assert "no longer assumes LONDON" in out["error"] \
            or "say which you meant" in out["error"], out
        return
    assert cachemap.town_slug(out["town"]) == town, out


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
    print("\n%s" % ("the town is never assumed" if not failures
                    else "%d FAILURES" % failures))
    sys.exit(1 if failures else 0)
