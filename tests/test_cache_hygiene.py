"""Three defects found by taking one accident seriously instead of writing a rule.

    python3 tests/test_cache_hygiene.py
    python3 -m pytest tests/test_cache_hygiene.py -v     # if pytest is installed

The accident: a ``warm`` run against the real user cache wrote a zero-sale street
file, and two test files plus both surface suites started failing. The tempting
response was a note telling the next person to isolate ``GAFF_CACHE_DIR``. The
note would have hidden three real defects.

1. **A zero-sale cache file counted as coverage.** ``_comps_towns`` listed street
   slugs by filename and never looked inside, so a street fetched successfully
   that came back with nothing — a misspelling, a road with no transactions, a
   new-build estate — made that street name "belong" to that town.
   ``_resolve_pool_town`` routes by street uniqueness, so an otherwise
   unplaceable subject was routed to London on the strength of a file containing
   nothing, and its clean refusal degraded to a weaker one. THE SHIPPED WARM
   CACHE CARRIES THREE OF THESE (penthouse, shreiber-house, st-oswald-s-place),
   so ``coverage`` overstated London by three streets in the released v0.1.0.

2. **``GAFF_CACHE_DIR`` isolation was silently partial.** ``landreg.CACHE_DIR``,
   ``hpi.CACHE_DIR`` and ``epc.EPC_CACHE_DIR`` were bound at IMPORT, so setting
   the variable afterwards moved ``paths.*`` but not them: reads through
   ``paths.read_candidates`` followed the new root while those modules kept
   using the old one. Every test that isolated itself that way was only half
   isolated, including four written the same evening.

3. **The suite depended on the developer's real cache**, and so did CI, which
   sets no cache directory and is isolated only because a fresh runner has no
   ``~/.gaff``.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import cachemap, epc, hpi, landreg, paths, tools  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Cache:
    """A temp user cache, set AFTER import — which is the case that was broken."""

    def __init__(self, seed=None):
        self._seed = seed or {}

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="gaff-hygiene-")
        self._old = os.environ.get(paths.ENV_CACHE_DIR)
        os.environ[paths.ENV_CACHE_DIR] = self.dir
        for rel, blob in self._seed.items():
            path = os.path.join(self.dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop(paths.ENV_CACHE_DIR, None)
        else:
            os.environ[paths.ENV_CACHE_DIR] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


EMPTY_STREET = {"cacheSchema": 1, "street": "NOWHERE LANE", "town": "LONDON",
                "url": "x", "fetchedAt": "2026-09-03T22:32:52Z",
                "count": 0, "items": []}
UNPLACEABLE = {"address": "Nowhere Lane, ATLANTIS", "beds": 2, "sqft": 800,
               "price": 300000, "mode": "buy"}


# ---------------------------------------------------------------------------
# 1. A cache file with no sales in it is not coverage.
# ---------------------------------------------------------------------------

def test_a_warmed_empty_street_does_not_route_a_listing():
    """The defect exactly: warming a street that has no recorded sales made an
    unplaceable subject placeable, on evidence containing nothing."""
    with _Cache({"comps/london/nowhere-lane.json": EMPTY_STREET}):
        listing = tools._ingest(dict(UNPLACEABLE), None)
        assert tools._resolve_pool_town(listing) is None
        payload = tools.value_check(fields=dict(UNPLACEABLE))
        assert "warmed town" in payload["error"]      # the CLEAN refusal, not
        assert "verifiably reach" not in payload["error"]   # the degraded one
        assert "tag" not in payload


def test_a_warmed_empty_street_is_not_counted_as_coverage():
    with _Cache({"comps/london/nowhere-lane.json": EMPTY_STREET}) as cache:
        assert "nowhere-lane" not in tools._comps_towns().get("london", set())
        assert "nowhere-lane" in tools._empty_streets("london")
        # include_empty is the raw listing, for anything that wants both.
        assert "nowhere-lane" in tools._comps_towns(include_empty=True)["london"]
        london = tools.coverage()["comps_towns"]["london"]
        assert "nowhere-lane" in london["fetchedButEmpty"]
        assert london["streets"] == len(tools._comps_towns()["london"])
        assert cache      # (keep the cache alive for the whole block)


def test_the_shipped_warm_cache_has_empty_streets_and_they_are_named():
    """Not hypothetical: v0.1.0 shipped three. The count must exclude them and
    the payload must still name them, or the information is simply lost."""
    empty = tools._empty_streets("london")
    assert empty, "expected the shipped London cache to carry empty streets"
    for slug in empty:
        assert slug not in tools._comps_towns()["london"]
    assert set(empty) <= set(tools.coverage()["comps_towns"]["london"]["fetchedButEmpty"])


def test_price_check_explains_an_empty_street_instead_of_going_quiet():
    with _Cache({"comps/london/nowhere-lane.json": EMPTY_STREET}):
        out = tools.price_check("Nowhere Lane", "LONDON")
        assert out["sales_found"] == 0
        assert "no recorded sales" in out["note"]
        assert "2026-09-03" in out["note"]           # when it was fetched
        assert "does not route a listing" in out["note"]


def test_emptiness_is_read_from_the_head_of_the_file_not_a_full_parse():
    """0.46 ms across London's 28 streets versus 8.5 ms to parse them, on an
    8 ms value_check. The cheap check is what makes this affordable at all."""
    with _Cache({"comps/london/nowhere-lane.json": EMPTY_STREET}) as cache:
        empty = os.path.join(cache.dir, "comps", "london", "nowhere-lane.json")
        assert tools._street_has_sales(empty) is False
        real = paths.read_path("comps", "london", "de-beauvoir-road.json")
        assert tools._street_has_sales(real) is True
        assert tools._street_has_sales(os.path.join(cache.dir, "missing.json")) is False


# ---------------------------------------------------------------------------
# 1b. A cache file that cannot be READ is not coverage either.
#
# Found 4 Sep 2026, live in the real user cache: a test had written
# ~/.gaff/cache/comps/testtown/bad-street.json containing the literal text
# `{not json`, and `coverage` reported "testtown: 2". The rule was "coverage
# unless I can see a zero count", so anything unparseable sailed through it as a
# street WITH sales -- counted in coverage, and able to route a listing through
# _resolve_pool_town's street-uniqueness branch.
#
# The rule is now positive: a file is coverage only if a positive count can
# actually be read out of its head. And an unreadable file is NOT folded in with
# the fetched-but-empty ones, because their explanation ("HM Land Registry holds
# no sales for them") would be a fresh false claim about a file nobody has
# managed to read.
# ---------------------------------------------------------------------------

CORRUPT = "{not json"
NO_COUNT = '{"town": "TESTTOWN", "items": [{"pricePaid": 2}]}'
TRUNCATED = '{"cacheSchema": 1, "street": "HALF ROAD", "town": "LONDON", "cou'


def _raw(cache, rel, text):
    """Seed a file _Cache cannot: raw bytes, not a JSON dump."""
    path = os.path.join(cache.dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_an_unreadable_cache_file_is_not_coverage():
    for label, text in (("corrupt", CORRUPT), ("no count key", NO_COUNT),
                        ("truncated", TRUNCATED)):
        with _Cache() as cache:
            path = _raw(cache, "comps/testtown/bad-street.json", text)
            assert tools._street_has_sales(path) is False, label
            assert "bad-street" not in tools._comps_towns().get("testtown", set()), label


def test_an_unreadable_file_is_not_called_fetched_but_empty():
    """"Fetched successfully and HM Land Registry holds no sales for them" is a
    claim about the upstream. Attaching it to a file we could not read would be
    inventing a second fact to cover for the first."""
    with _Cache() as cache:
        _raw(cache, "comps/testtown/bad-street.json", CORRUPT)
        assert "bad-street" not in tools._empty_streets("testtown")
        town = tools.coverage()["comps_towns"]["testtown"]
        assert "bad-street" not in town.get("fetchedButEmpty", [])
        assert "bad-street" in town.get("unreadable", []), town
        assert "could not be read" in town.get("unreadableNote", "")


def test_an_unreadable_file_does_not_route_a_listing():
    """The harm, not the bookkeeping. A street held only by an unreadable file
    must not make an unplaceable subject placeable."""
    with _Cache() as cache:
        _raw(cache, "comps/testtown/nowhere-lane.json", CORRUPT)
        listing = tools._ingest(dict(UNPLACEABLE), None)
        assert tools._resolve_pool_town(listing) is None


def test_the_three_states_are_told_apart_by_name():
    with _Cache() as cache:
        real = paths.read_path("comps", "london", "de-beauvoir-road.json")
        empty = os.path.join(cache.dir, "comps", "london", "nowhere-lane.json")
        os.makedirs(os.path.dirname(empty), exist_ok=True)
        with open(empty, "w", encoding="utf-8") as fh:
            json.dump(EMPTY_STREET, fh)
        bad = _raw(cache, "comps/london/bad-street.json", CORRUPT)
        assert cachemap.street_state(real) == "sales"
        assert cachemap.street_state(empty) == "empty"
        assert cachemap.street_state(bad) == "unreadable"
        assert cachemap.street_state(os.path.join(cache.dir, "gone.json")) == "unreadable"


def test_a_positive_count_is_still_read_from_the_head_not_a_full_parse():
    """The fix must not cost the cheap check: the reason emptiness is read from
    512 bytes is 0.46 ms across London's streets against 8.5 ms to parse them.
    Every one of the 39 cached files carries its count within 314 bytes."""
    real = paths.read_path("comps", "london", "de-beauvoir-road.json")
    head = open(real, "rb").read(512)
    assert cachemap._COUNT_IN_HEAD.search(head), \
        "the count must be findable in the first 512 bytes or the fast path is a lie"


# ---------------------------------------------------------------------------
# 2. Setting GAFF_CACHE_DIR after import moves EVERYTHING.
# ---------------------------------------------------------------------------

def test_the_cache_directories_are_resolved_on_use_not_captured_at_import():
    """These were module constants bound at import, so a variable set afterwards
    moved paths.* and not them — silent partial isolation."""
    with _Cache() as cache:
        for module, name in ((landreg, "CACHE_DIR"), (landreg, "SHIPPED_CACHE_DIR"),
                             (hpi, "CACHE_DIR"), (epc, "EPC_CACHE_DIR")):
            resolved = getattr(module, name)
            if name.startswith("SHIPPED"):
                continue                     # the shipped tier is not per-user
            assert resolved.startswith(cache.dir), \
                "%s.%s did not follow GAFF_CACHE_DIR: %s" % (
                    module.__name__, name, resolved)


def test_assignment_still_wins_because_an_existing_test_relies_on_it():
    """tests/test_epc.py isolates itself by assigning these globals directly
    ("monkeypatched globals are read at call time"). Making them lazy must not
    take that away."""
    tmp = tempfile.mkdtemp(prefix="gaff-override-")
    try:
        epc.EPC_CACHE_DIR = tmp
        assert epc._search_cache_path("N1 9ZY").startswith(tmp)
    finally:
        del epc.EPC_CACHE_DIR              # back to lazy resolution
        shutil.rmtree(tmp, ignore_errors=True)
    with _Cache() as cache:
        assert epc._search_cache_path("N1 9ZY").startswith(cache.dir)


def test_no_module_binds_a_cache_directory_at_import():
    """The check that keeps this true: a bare CACHE_DIR name anywhere in these
    modules means someone reintroduced the capture."""
    names = {"CACHE_DIR", "SHIPPED_CACHE_DIR", "EPC_CACHE_DIR", "SHIPPED_EPC_DIR"}
    offenders = []
    for rel in ("gaff_engine/landreg.py", "gaff_engine/hpi.py", "gaff_engine/epc.py"):
        tree = ast.parse(open(os.path.join(_ROOT, rel), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in names:
                offenders.append("%s:%d %s" % (rel, node.lineno, node.id))
    assert not offenders, (
        "these read a cache directory as a bare name, which binds it at import: "
        "%s. Use _root('<NAME>') so it resolves on use." % offenders)


# ---------------------------------------------------------------------------
# 3. The suite does not depend on what is in the developer's real cache.
# ---------------------------------------------------------------------------

def _snapshot_real_cache():
    """path -> (size, mtime) for everything under the real user cache.

    Deliberately reads paths.user_cache_dir() with the environment as the SUITE
    RUNNER left it, not as a test set it: the property under test is that a
    subprocess told to use a temp cache does not touch the developer's own.
    """
    root = os.path.join(os.path.expanduser("~"), ".gaff", "cache")
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out[full] = (st.st_size, st.st_mtime_ns)
    return out


def _run_suite_against(cache_dir):
    """Run every test file and both surface suites with GAFF_CACHE_DIR pointed
    at ``cache_dir``; return the ones that fail."""
    env = dict(os.environ, GAFF_CACHE_DIR=cache_dir)
    failed = []
    targets = [os.path.join("tests", f) for f in sorted(os.listdir(os.path.join(_ROOT, "tests")))
               if f.startswith("test_") and f.endswith(".py")
               and f != os.path.basename(__file__)]          # not ourselves
    for d in ("spike", "surfaces"):
        for f in ("mcp_client_test.py", "cli_test.py"):
            path = os.path.join(d, f)
            if os.path.exists(os.path.join(_ROOT, path)):
                targets.append(path)
    for target in targets:
        proc = subprocess.run([sys.executable, target], cwd=_ROOT, env=env,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            failed.append(target)
    return failed


def test_a_populated_user_cache_does_not_change_any_result():
    """The accident, replayed. A warmed-empty street and a saved session are
    both things a real user's cache will hold; neither may move a test."""
    cache = tempfile.mkdtemp(prefix="gaff-populated-")
    try:
        street = os.path.join(cache, "comps", "london", "nowhere-lane.json")
        os.makedirs(os.path.dirname(street), exist_ok=True)
        with open(street, "w", encoding="utf-8") as fh:
            json.dump(EMPTY_STREET, fh)
        env = dict(os.environ, GAFF_CACHE_DIR=cache)
        subprocess.run(
            [sys.executable, "-c",
             "from gaff_engine import session; "
             "session.save(session.search_from_answers("
             "{'mode':'buy','town':'LONDON','constraints':['min_beds>=4']}), None)"],
            cwd=_ROOT, env=env, check=True, capture_output=True)
        # Snapshot the REAL user cache around the run. This is how the three
        # test-residue files got into ~/.gaff on 3 Sep: a test wrote there
        # instead of into its temp directory, and nothing noticed until
        # `coverage` started reporting a town called "testtown" whose two files
        # were `{not json` and a two-field fake. A suite that can write outside
        # its sandbox will do it again; this is the tripwire.
        before = _snapshot_real_cache()
        failed = _run_suite_against(cache)
        assert not failed, "a populated user cache changed these: %s" % failed
        after = _snapshot_real_cache()
        added = sorted(set(after) - set(before))
        changed = sorted(k for k in set(after) & set(before) if after[k] != before[k])
        assert not added and not changed, (
            "the suite wrote to the REAL user cache despite GAFF_CACHE_DIR "
            "pointing elsewhere. added=%s changed=%s" % (added, changed))
    finally:
        shutil.rmtree(cache, ignore_errors=True)


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
    print("\n%s" % ("cache hygiene holds" if not failures
                    else "%d FAILURES" % failures))
    sys.exit(1 if failures else 0)
