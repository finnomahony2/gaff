"""S4 tests — the offline promise, measured against the running code.

    python3 tests/test_netgate.py
    python3 -m pytest tests/test_netgate.py -v     # if pytest is installed

The test that matters is :func:`test_the_declared_set_equals_the_set_that_can_
open_a_socket`. It does not assert against a list someone typed twice. It RUNS
every registered tool with the connect path denied, records which ones reached
for it, and compares that measured set to ``netgate.NETWORK_VERBS``. A new
fetcher that forgets to declare itself fails here rather than quietly breaking
the promise.

Writing it found a real breach — ``value.py`` reached ``hpi.hpi_factor`` without
``offline=True``, so a comp from a month the HPI cache lacks would have had
``value_check`` open a socket. ``test_a_cold_hpi_cache_does_not_make_a_verdict_
fetch`` is that failure, pinned.

How the deny works. It patches ``socket.socket.connect`` and friends, NOT
``socket.socket`` itself: ``ssl.SSLSocket`` subclasses ``socket.socket`` at
import time, so replacing the class breaks the import of ``urllib`` before a
single tool runs. Denying at connect also catches every transport above it —
urllib, http.client, anything a future fetcher might reach for.
"""

import copy
import os
import shutil
import socket
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import everything BEFORE the deny is installed (see the module docstring).
from gaff_engine import (  # noqa: E402
    epc, flips, hpi, landreg, netgate, paths, tools, value,
)


class SocketOpened(Exception):
    """A connect was attempted while the network was denied."""


class _NoNetwork:
    """Deny every connect path, and isolate the user cache while doing it."""

    def __enter__(self):
        self.attempts = []
        self._dir = tempfile.mkdtemp(prefix="gaff-netgate-test-")
        self._old_cache = os.environ.get(paths.ENV_CACHE_DIR)
        os.environ[paths.ENV_CACHE_DIR] = self._dir

        def deny(*args, **kwargs):
            self.attempts.append(args[1] if len(args) > 1 else args)
            raise SocketOpened("a socket connect was attempted")

        self._orig = (socket.socket.connect, socket.socket.connect_ex,
                      socket.create_connection, socket.getaddrinfo)
        socket.socket.connect = deny
        socket.socket.connect_ex = deny
        socket.create_connection = deny
        socket.getaddrinfo = deny
        return self

    def __exit__(self, *exc):
        (socket.socket.connect, socket.socket.connect_ex,
         socket.create_connection, socket.getaddrinfo) = self._orig
        if self._old_cache is None:
            os.environ.pop(paths.ENV_CACHE_DIR, None)
        else:
            os.environ[paths.ENV_CACHE_DIR] = self._old_cache
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


# ---------------------------------------------------------------------------
# How each tool is exercised. Every entry must reach the tool's REAL work: a
# call that bails on a usage error before its network path proves nothing, so
# warm is driven down both of its paths with a street and a town nothing has
# cached.
# ---------------------------------------------------------------------------

AXES = ["light_and_volume", "outdoor_space", "character_bones",
        "width_proportion_flow", "street_scene", "raw_size_threshold",
        "design_finish", "station_proximity"]
READS = {"axes": {a: {"score": 7.0, "contribution": "c"} for a in AXES}}
LONDON = {"address": "De Beauvoir Road, London N1", "beds": 2, "baths": 2,
          "sqft": 1050, "price": "£1,250,000", "property_type": "maisonette"}
LEAMINGTON = {"address": "Willes Road, Leamington Spa", "beds": 2, "baths": 1,
              "sqft": 780, "price": "£365,000", "property_type": "flat"}
RENTAL = {"address": "De Beauvoir Road, London N1", "beds": 2,
          "rent_pcm": "£2,400"}

INVOCATIONS = {
    # The front door, driven both ways a host will call it: fully stated, and
    # empty. Both reach the cache walk, the pool read and the token check,
    # which are the three places situate could grow a fetch.
    "situate": [dict(mode="buy", nation="england", town="LONDON",
                     budget_max=750000, constraints=["min_beds>=2"]),
                {}],
    "price_check": [dict(street="De Beauvoir Road", town="LONDON"),
                    dict(street="Nowhere Lane", town="LONDON")],   # cache miss
    "flip_stats": [dict(town="LEAMINGTON SPA"), dict(town="TOTNES")],
    "read_listing": [dict(fields=dict(LONDON))],
    "value_check": [dict(fields=dict(LONDON)), dict(fields=dict(LEAMINGTON))],
    "taste_score": [dict(reads=READS, fields=dict(LONDON))],
    "score_listing": [dict(fields=dict(LONDON), reads=READS),
                      dict(fields=dict(LEAMINGTON), reads=READS)],
    "show_work": [dict(fields=dict(LONDON), reads=READS)],
    "rent_check": [dict(fields=dict(RENTAL))],
    "coverage": [{}],
    "warm": [dict(street="Nowhere Lane", town="LONDON"),   # the street path
             dict(flips_town="TOTNES")],                   # the flips path
}


def _measure():
    """Run every registered tool under the deny; return the set that reached
    for a socket. A cold-data refusal is fine — that IS the offline behaviour
    under test. Only an ATTEMPTED CONNECT counts.

    A bad INVOCATION is not fine and is raised, not swallowed: a tool that bails
    on a usage error before reaching its network path measures as "offline" and
    would let a real fetcher through unnoticed.
    """
    reached = set()
    with _NoNetwork() as net:
        for name, fn in sorted(tools.DISPATCH.items()):
            for kwargs in INVOCATIONS[name]:
                before = len(net.attempts)
                try:
                    fn(**kwargs)
                except SocketOpened:
                    pass
                except (tools.UsageError, TypeError) as exc:
                    raise AssertionError(
                        "the invocation for %s is wrong (%s: %s) — it never "
                        "reached the tool's work, so measuring it proves "
                        "nothing" % (name, type(exc).__name__, exc))
                except Exception:                          # noqa: BLE001
                    pass          # a cold-data refusal is the point, not a fail
                if len(net.attempts) > before:
                    reached.add(name)
    return reached


# ---------------------------------------------------------------------------
# THE test.
# ---------------------------------------------------------------------------

def test_every_registered_tool_has_an_invocation_here():
    """Otherwise a new tool could be added and simply never measured — the
    quiet way this test stops being true."""
    assert set(INVOCATIONS) == set(tools.DISPATCH), \
        "add an invocation for: %s" % sorted(set(tools.DISPATCH) - set(INVOCATIONS))


def test_the_declared_set_equals_the_set_that_can_open_a_socket():
    measured = _measure()
    declared = set(netgate.NETWORK_TOOLS)
    undeclared = sorted(measured - declared)
    assert not undeclared, (
        "these tools opened a socket but are not declared in netgate."
        "NETWORK_VERBS: %s. Either they should not fetch, or the declaration "
        "must say what they send and to whom." % undeclared)
    unproven = sorted(declared - measured)
    assert not unproven, (
        "these tools are declared as network verbs but never reached for a "
        "socket: %s. Either the declaration is stale, or the invocation in "
        "INVOCATIONS does not reach the fetch path — a declaration nothing "
        "exercises is not a promise, it is a comment." % unproven)


def test_warm_is_the_one_and_both_of_its_paths_fetch():
    with _NoNetwork() as net:
        for kwargs in INVOCATIONS["warm"]:
            before = len(net.attempts)
            try:
                tools.warm(**kwargs)
            except Exception:                              # noqa: BLE001
                pass
            assert len(net.attempts) > before, \
                "warm(%s) did not reach the network" % kwargs
    assert sorted(netgate.NETWORK_VERBS) == ["warm"]
    assert len(netgate.verbs("warm")) == 2                 # street, and flips


# ---------------------------------------------------------------------------
# The breach this check found, pinned so it cannot come back.
# ---------------------------------------------------------------------------

def test_a_cold_hpi_cache_does_not_make_a_verdict_fetch():
    """value.py called hpi.hpi_factor without offline=True. No verdict fetched,
    but only because the shipped HPI cache happens to cover the months the two
    warmed towns' comps fall in. A comp from a month it lacks — an old sale, a
    new region, a rewarmed town — made value_check, a tool documented as
    entirely offline, open a socket."""
    with _NoNetwork():
        assert hpi.hpi_factor("hackney", "flat", "1994-01-15", offline=True) == 1.0
        listing = tools._ingest(dict(LONDON), None)
        comps = list(value.load_enriched_comps())
        ancient = copy.deepcopy(comps[0])
        ancient.date = "1994-03-11"          # a month no shipped cache can hold
        verdict = value.value_verdict(listing, comps + [ancient])
        assert verdict.tag is not None       # answered, and never went out


def test_the_hpi_default_is_still_fetching_which_is_why_the_call_sites_force_it():
    """Not a bug in hpi: fetch_month exists to fetch, and warm's path wants it.
    The promise lives at the CALL SITES, which is exactly why it needs a test
    that measures rather than a default that looks safe."""
    with _NoNetwork():
        try:
            hpi.avg_price("hackney", "flat", "1994-01")
        except SocketOpened:
            return
    raise AssertionError(
        "hpi.avg_price no longer fetches by default — if that is deliberate, "
        "this test and the offline=True call sites in value.py should be "
        "revisited together")


# ---------------------------------------------------------------------------
# What a declaration has to say.
# ---------------------------------------------------------------------------

def test_a_declaration_names_what_it_sends_and_to_whom():
    """Consent is to a specific disclosure, not to "the network"."""
    for tool in netgate.NETWORK_VERBS:
        for d in netgate.verbs(tool):
            for field in ("sends", "to", "host", "why", "calls", "licence"):
                assert d.get(field), "%s: %s is empty" % (tool, field)
            assert d["host"] in netgate.HOSTS.values(), d["host"]


def test_the_declared_hosts_are_the_endpoints_the_fetchers_actually_use():
    """A declaration naming a host the code does not call is decoration."""
    assert netgate.HOSTS["land_registry"] in landreg.ENDPOINT
    assert netgate.HOSTS["land_registry"] in hpi.BASE
    assert netgate.HOSTS["epc_register"] in epc.API_BASE
    # flips reaches the register through landreg's endpoint, not its own.
    assert flips.ENDPOINT == landreg.ENDPOINT


def test_the_consent_line_is_one_plain_sentence():
    line = netgate.consent_line("warm", 0)
    assert "a street name and a town" in line
    assert "landregistry.data.gov.uk" in line
    assert "local caches" in line
    assert netgate.consent_line("warm", 1) != line          # the flips path
    # An offline tool has no consent line to render, and says so with None
    # rather than an empty string a caller would print.
    assert netgate.consent_line("value_check") is None
    assert netgate.consent_line("warm", 99) is None


def test_warm_shows_the_consent_line_before_it_goes_out():
    """The helper the plan asks for, actually called by the fetcher: a user
    sees what is about to be sent and to whom, not just "fetching"."""
    seen = []
    with _NoNetwork():
        for kwargs in INVOCATIONS["warm"]:
            try:
                tools.warm(progress=seen.append, **kwargs)
            except Exception:                              # noqa: BLE001
                pass
    joined = " | ".join(seen)
    assert "a street name and a town" in joined
    assert "a town name" in joined
    assert "landregistry.data.gov.uk" in joined
    assert "local caches" in joined


def test_the_promise_is_derived_from_the_list_not_retyped():
    note = netgate.offline_note()
    assert note.startswith("warm is the only tool that touches the network")
    assert netgate.manifest() and len(netgate.manifest()) == 2
    assert all(entry["line"] for entry in netgate.manifest())


def test_declares_and_verbs_are_honest_about_the_offline_tools():
    assert netgate.declares("warm") is True
    for name in tools.DISPATCH:
        if name != "warm":
            assert netgate.declares(name) is False, name
            assert netgate.verbs(name) == ()


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
    print("\n%s" % ("the offline promise holds" if not failures
                    else "%d FAILURES" % failures))
    sys.exit(1 if failures else 0)
