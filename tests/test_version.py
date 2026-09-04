"""One version, and the three places that have to agree.

    python3 tests/test_version.py
    python3 -m pytest tests/test_version.py -v     # if pytest is installed

Why this file exists, and it is not hypothetical. Until 4 September 2026 the
version was written as three independent literals:

    gaff_engine/__init__.py     __version__ = "0.3.0"   an internal milestone
    gaff_engine/mcp.py          serverInfo  = "0.1.0"   hard-coded in the frame
    public/pyproject.toml       version     = "0.1.0"   what pip installs as

They disagreed IN THE PUBLISHED ARTEFACT. Anyone who installed the released
v0.1.0 and asked it its version was told 0.3.0. Nothing caught it because
nothing compared them.

There is now one literal (``gaff_engine.__version__``); ``mcp`` reports it and
``pyproject`` must match it. pyproject cannot import, so it stays a separate
literal and this file is what keeps it honest.

A related trap the same pass found: ``tools/build_bundle.py`` reads the version
out of the assembled tree's pyproject, so a bundle built from an unbumped tree
declares itself a version that is already taken by different bytes. Bumping
pyproject is therefore not cosmetic — it is what makes a bundle identifiable.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gaff_engine  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The lab keeps packaging metadata under public/; the assembled tree has it at
#: the root (build_public copies public/pyproject.toml -> pyproject.toml). Both
#: layouts run this suite, so look in both — the same walk tests/test_flips.py
#: and tests/test_registry.py do for spike/ versus surfaces/.
_PYPROJECT_CANDIDATES = (os.path.join(_ROOT, "public", "pyproject.toml"),
                         os.path.join(_ROOT, "pyproject.toml"))


def _pyproject_version():
    for path in _PYPROJECT_CANDIDATES:
        if not os.path.exists(path):
            continue
        m = re.search(r'^version\s*=\s*"([^"]+)"', open(path, encoding="utf-8").read(),
                      re.M)
        if m:
            return m.group(1), path
    return None, None


def test_the_version_is_a_plain_release_number():
    assert re.match(r"^\d+\.\d+\.\d+$", gaff_engine.__version__), \
        "%r is not a release number" % gaff_engine.__version__


def test_pyproject_agrees_with_the_package():
    version, path = _pyproject_version()
    assert version is not None, "no pyproject.toml found in %s" % (_PYPROJECT_CANDIDATES,)
    assert version == gaff_engine.__version__, (
        "%s says %s and gaff_engine.__version__ says %s. A wheel that installs "
        "as one version and reports another is what shipped in v0.1.0."
        % (path, version, gaff_engine.__version__))


def test_the_mcp_handshake_reports_the_package_version():
    """Read out of the module rather than asserted against a literal: a literal
    here would just be a fourth copy to drift."""
    src = open(os.path.join(_ROOT, "gaff_engine", "mcp.py"), encoding="utf-8").read()
    assert '"version": __version__' in src, \
        "mcp.py's serverInfo must report gaff_engine.__version__, not a literal"
    assert not re.search(r'"version":\s*"\d+\.\d+\.\d+"', src), \
        "mcp.py has a hard-coded version again"


def test_the_milestone_note_survived_the_bump():
    """__version__ used to carry 'M1 complete' as a trailing comment. That said
    something the release number cannot, so it kept its own name rather than
    being deleted along with the number it was attached to."""
    assert getattr(gaff_engine, "MILESTONE", "").startswith("M1 complete")


def test_the_running_server_reports_it_over_the_wire():
    """The handshake a host actually sees, not the source that produces it."""
    from gaff_engine import mcp
    seen = {}
    original = mcp._result
    try:
        mcp._result = lambda rid, payload: seen.update(payload)
        mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    finally:
        mcp._result = original
    assert seen["serverInfo"]["version"] == gaff_engine.__version__
    assert seen["serverInfo"]["name"] == "gaff"


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
    print("\n%s" % ("one version, three places agree" if not failures
                    else "%d FAILURES" % failures))
    sys.exit(1 if failures else 0)
