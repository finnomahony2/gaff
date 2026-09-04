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

from gaff_engine import epc, hpi, landreg, paths, tools  # noqa: E402

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
        failed = _run_suite_against(cache)
        assert not failed, "a populated user cache changed these: %s" % failed
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
