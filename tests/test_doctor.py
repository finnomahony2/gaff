"""T13 tests — gaff doctor: paste-able, offline, and above all secret-free.

    python3 -m pytest tests/test_doctor.py -v
    python3 tests/test_doctor.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import doctor  # noqa: E402


def test_report_carries_every_section():
    text, _failed = doctor.report()
    for needle in ("== gaff doctor ==", "python", "cache tiers", "shipped data",
                   "epc token", "offline self-checks", "paste this whole block"):
        assert needle in text, needle


def test_selfchecks_pass_on_a_healthy_checkout():
    text, failed = doctor.report()
    assert not failed, text


def test_token_value_never_appears_even_when_env_is_set():
    """The one property that must survive every future edit: a real token in
    the environment does not reach the output, only its source name does."""
    sentinel = "tok-SECRET-do-not-print-9x7"
    old = os.environ.get("GAFF_EPC_TOKEN")
    os.environ["GAFF_EPC_TOKEN"] = sentinel
    try:
        text, _failed = doctor.report()
        assert sentinel not in text
        assert "env GAFF_EPC_TOKEN (set)" in text
    finally:
        if old is None:
            os.environ.pop("GAFF_EPC_TOKEN", None)
        else:
            os.environ["GAFF_EPC_TOKEN"] = old


def test_broken_install_reports_fail_instead_of_crashing():
    """A failing self-check must land as a FAIL line — the doctor's whole
    point is producing a report ABOUT breakage, not breaking on it."""
    import gaff_engine.landreg as L
    old_user, old_ship = L.CACHE_DIR, L.SHIPPED_CACHE_DIR
    L.CACHE_DIR = L.SHIPPED_CACHE_DIR = "/nonexistent-gaff-doctor-test"
    try:
        text, failed = doctor.report()
        assert failed and "FAIL  comps read" in text
        assert "== gaff doctor ==" in text            # the rest still rendered
    finally:
        L.CACHE_DIR, L.SHIPPED_CACHE_DIR = old_user, old_ship


def test_undecodable_token_file_cannot_kill_the_report():
    """F1 regression: a garbage-encoded token file is a diagnosis, not a
    crash — the report must still render in full."""
    import shutil
    import tempfile
    home = tempfile.mkdtemp()
    old_home, old_tok = os.environ.get("HOME"), os.environ.get("GAFF_EPC_TOKEN")
    os.environ["HOME"] = home
    os.environ.pop("GAFF_EPC_TOKEN", None)
    try:
        os.makedirs(os.path.join(home, ".gaff"))
        with open(os.path.join(home, ".gaff", "epc_token"), "wb") as fh:
            fh.write(b"\xff\xfe\x00tok")
        text, _failed = doctor.report()
        assert "== gaff doctor ==" in text
        assert "epc token" in text
    finally:
        os.environ["HOME"] = old_home
        if old_tok is not None:
            os.environ["GAFF_EPC_TOKEN"] = old_tok
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
