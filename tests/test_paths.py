"""Tests for gaff_engine.paths — the on-disk location resolver (E1).

DETERMINISTIC: no network. Every case runs against temp dirs with the
environment restored afterwards, so the real ``~/.gaff`` is never touched.

    python3 -m pytest tests/test_paths.py -v
    python3 tests/test_paths.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import paths  # noqa: E402


class _Env(object):
    """Set env vars for a block and restore exactly what was there before."""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


# ---------------------------------------------------------------------------
# 1 · The two tiers resolve, and the env overrides them.
# ---------------------------------------------------------------------------

def test_user_cache_defaults_under_home():
    with _Env(GAFF_CACHE_DIR=None):
        assert paths.user_cache_dir() == os.path.join(
            os.path.expanduser("~"), ".gaff", "cache")


def test_env_overrides_user_cache_and_expands_home():
    with _Env(GAFF_CACHE_DIR="~/somewhere-else"):
        assert paths.user_cache_dir() == os.path.join(
            os.path.expanduser("~"), "somewhere-else")


def test_empty_env_var_is_treated_as_unset():
    """An exported-but-empty var must not resolve paths against "/"."""
    with _Env(GAFF_CACHE_DIR="   "):
        assert paths.user_cache_dir() == os.path.join(
            os.path.expanduser("~"), ".gaff", "cache")


def test_shipped_data_dir_is_a_real_directory():
    assert os.path.isdir(paths.shipped_data_dir())


# ---------------------------------------------------------------------------
# 2 · Reads prefer the user cache; writes never leave it.
# ---------------------------------------------------------------------------

def test_read_falls_back_to_shipped_when_user_cache_is_empty():
    user, shipped = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(shipped, "comps"))
        seeded = os.path.join(shipped, "comps", "x.json")
        open(seeded, "w").write("{}")
        with _Env(GAFF_CACHE_DIR=user, GAFF_DATA_DIR=shipped):
            assert paths.read_path("comps", "x.json") == seeded
    finally:
        shutil.rmtree(user, ignore_errors=True)
        shutil.rmtree(shipped, ignore_errors=True)


def test_user_cache_shadows_shipped():
    """The user's own copy wins, so a fresh fetch is never masked by shipped."""
    user, shipped = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        for root in (user, shipped):
            os.makedirs(os.path.join(root, "comps"))
            open(os.path.join(root, "comps", "x.json"), "w").write("{}")
        with _Env(GAFF_CACHE_DIR=user, GAFF_DATA_DIR=shipped):
            assert paths.read_path("comps", "x.json") == \
                os.path.join(user, "comps", "x.json")
    finally:
        shutil.rmtree(user, ignore_errors=True)
        shutil.rmtree(shipped, ignore_errors=True)


def test_read_path_returns_none_when_nothing_exists():
    user, shipped = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        with _Env(GAFF_CACHE_DIR=user, GAFF_DATA_DIR=shipped):
            assert paths.read_path("comps", "nope.json") is None
    finally:
        shutil.rmtree(user, ignore_errors=True)
        shutil.rmtree(shipped, ignore_errors=True)


def test_write_path_lands_in_user_cache_and_makes_parents():
    """Writes must never mutate the shipped tier, even if it is writable."""
    user, shipped = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        with _Env(GAFF_CACHE_DIR=user, GAFF_DATA_DIR=shipped):
            p = paths.write_path("comps", "london", "a-road.json")
        assert p == os.path.join(user, "comps", "london", "a-road.json")
        assert os.path.isdir(os.path.dirname(p))       # parents created
        assert not os.listdir(shipped)                 # shipped untouched
    finally:
        shutil.rmtree(user, ignore_errors=True)
        shutil.rmtree(shipped, ignore_errors=True)


def test_read_candidates_are_user_then_shipped_and_deduped():
    one = tempfile.mkdtemp()
    try:
        with _Env(GAFF_CACHE_DIR=one, GAFF_DATA_DIR=one):
            # Both tiers pointing at the same root must not yield it twice.
            assert paths.read_candidates("epc", "c.json") == \
                [os.path.join(one, "epc", "c.json")]
    finally:
        shutil.rmtree(one, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3 · Token resolution — order, and a failure that leaks nothing.
# ---------------------------------------------------------------------------

def test_env_token_wins():
    with _Env(GAFF_EPC_TOKEN="tok-from-env"):
        assert paths.epc_token() == "tok-from-env"


def test_missing_token_raises_naming_every_source_and_no_value():
    """The error must be actionable (name each source) and leak no secret."""
    empty = tempfile.mkdtemp()
    try:
        with _Env(GAFF_EPC_TOKEN=None, HOME=empty):
            # Neutralise the keychain and the in-repo dev fallback so the
            # not-found branch is what is actually under test.
            kc, repo = paths._keychain_token, paths._REPO_ROOT
            paths._keychain_token = lambda: None
            paths._REPO_ROOT = empty
            try:
                paths.epc_token()
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                msg = str(e)
                assert "GAFF_EPC_TOKEN" in msg
                assert paths.KEYCHAIN_SERVICE in msg
                assert "~/.gaff/epc_token" in msg
                assert "epc.opendatacommunities.org" in msg
            finally:
                paths._keychain_token, paths._REPO_ROOT = kc, repo
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def test_token_file_fallback_is_read_and_stripped():
    home = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(home, ".gaff"))
        open(os.path.join(home, ".gaff", "epc_token"), "w").write("  tok-file \n")
        with _Env(GAFF_EPC_TOKEN=None, HOME=home):
            kc = paths._keychain_token
            paths._keychain_token = lambda: None
            try:
                assert paths.epc_token() == "tok-file"
            finally:
                paths._keychain_token = kc
    finally:
        shutil.rmtree(home, ignore_errors=True)


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
