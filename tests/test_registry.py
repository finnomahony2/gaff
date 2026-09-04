"""S3 tests — the tool registry, and the promise that it changed nothing.

    python3 tests/test_registry.py
    python3 -m pytest tests/test_registry.py -v     # if pytest is installed

The first test is the point of the whole file: the ten tools were re-declared
through ``register()``, and the manifest a host reads must be byte-for-byte what
it was before. ``tests/fixtures/tools_manifest_v0.json`` was captured from the
hand-written literal immediately before the refactor and is the reference.
"""

import inspect
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import registry, tools  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_MANIFEST_V0 = os.path.join(_HERE, "fixtures", "tools_manifest_v0.json")
_ROOT = os.path.dirname(_HERE)


# ---------------------------------------------------------------------------
# The promise: the MCP surface did not churn.
# ---------------------------------------------------------------------------

#: Deliberate edits to a SHIPPED entry, each with the item that made it and the
#: reason. The fixture stays the record of what v0.1.0 said; this is the record
#: of what has been changed since, and why.
#:
#: Why a table rather than a regenerated fixture: a tool's description is the
#: only thing an MCP host reads before choosing it, so churn in it is a real
#: user-facing change and must cost somebody a decision. An entry here is that
#: decision, written down. Everything not listed is still byte-pinned, and a
#: listed tool is still byte-pinned — against the fixture with exactly these
#: substitutions applied — so a reword nobody chose still fails.
_CHANGED_SINCE_V0 = {
    "price_check": ("F-02: town= stopped defaulting to LONDON, so a "
                    "description promising that default described behaviour "
                    "the tool no longer has", {
        "Town, e.g. 'LONDON'. Defaults to LONDON.":
            "Town, e.g. 'LONDON'. Optional: left out, it is taken from the "
            "one warmed town holding that street, else from the saved "
            "search. If neither places it, the tool asks rather than "
            "assuming a city.",
    }),
    "warm": ("F-02: warm spends the live call, so it stopped defaulting to "
             "LONDON too — a fetch aimed at a guessed city spends the request "
             "and caches nothing useful", {
        "Town for street=. Defaults to LONDON.":
            "Town for street=. Optional only if a saved search names one; "
            "this call spends a live request, so it is never aimed at a "
            "guessed city.",
    }),
}


def test_every_v0_tool_is_byte_identical_to_the_hand_written_one():
    """The descriptions are user-facing text. A refactor may not reword them or
    change a single character of the JSON a host reads.

    Compared entry by entry rather than as one blob, because F-01 added an
    eleventh tool (``situate``) and the fixture is a RECORD of what v0.1.0
    shipped, not a snapshot to be regenerated whenever the surface grows. A new
    tool is not a rewording of the ten; a rewording of the ten still fails here.

    F-02 added the second kind of legitimate change: a shipped sentence that
    became FALSE when the behaviour under it changed. Those are listed in
    :data:`_CHANGED_SINCE_V0` with their reason and their replacement text, and
    the comparison is made against the fixture with those substitutions
    applied — so the changed tools are pinned just as tightly as the rest, only
    to a newer pin that somebody had to write down.
    """
    pinned = {t["name"]: t for t in json.loads(open(_MANIFEST_V0,
                                                   encoding="utf-8").read())}
    current = {t["name"]: t for t in tools.TOOLS}
    missing = sorted(set(pinned) - set(current))
    assert not missing, "these shipped tools have disappeared: %s" % missing
    assert not set(_CHANGED_SINCE_V0) - set(pinned), (
        "_CHANGED_SINCE_V0 names a tool the fixture never shipped")
    for name, entry in pinned.items():
        expected = json.dumps(entry, indent=1, ensure_ascii=False)
        _reason, edits = _CHANGED_SINCE_V0.get(name, (None, {}))
        for was, now in edits.items():
            assert was in expected, (
                "%s: _CHANGED_SINCE_V0 quotes %r, which is not in the v0 "
                "fixture — the record and the change list have drifted"
                % (name, was))
            expected = expected.replace(json.dumps(was)[1:-1],
                                        json.dumps(now)[1:-1])
        assert json.dumps(current[name], indent=1, ensure_ascii=False) == expected, (
            "%s's manifest entry changed; it is user-facing text. If the "
            "change was deliberate, add it to _CHANGED_SINCE_V0 with the item "
            "and the reason" % name)


#: What v0.1.0 shipped. Named here so a growing surface stays legible against it.
_V0_TOOLS = {"price_check", "flip_stats", "read_listing", "value_check",
             "taste_score", "score_listing", "show_work", "rent_check",
             "coverage", "warm"}


def test_the_tools_are_all_here_and_dispatch_to_callables():
    expected = _V0_TOOLS | {"situate"}                    # F-01's front door
    assert {t["name"] for t in tools.TOOLS} == expected
    assert set(tools.DISPATCH) == expected
    assert all(callable(fn) for fn in tools.DISPATCH.values())


def test_tools_and_dispatch_are_the_registrys_own_objects():
    """The surface tests monkeypatch these in place (gt.TOOLS.append(...),
    gt.DISPATCH['noisy'] = ...). Rebinding either to a copy would break both
    surfaces' tests silently."""
    assert tools.TOOLS is registry.TOOLS
    assert tools.DISPATCH is registry.DISPATCH
    # The shim is spike/ in the lab and surfaces/ in the assembled package
    # (tests/test_flips.py does the same walk).
    for d in ("spike", "surfaces"):
        if os.path.isdir(os.path.join(_ROOT, d)):
            sys.path.insert(0, os.path.join(_ROOT, d))
            break
    import gaff_tools                                        # noqa: E402
    assert gaff_tools.TOOLS is registry.TOOLS
    assert gaff_tools.DISPATCH is registry.DISPATCH


# ---------------------------------------------------------------------------
# What register() refuses. Each of these used to be discoverable only at
# call time, in front of a user.
# ---------------------------------------------------------------------------

def _fresh():
    """A registry whose global tables are restored after the block."""
    class _Sandbox:
        def __enter__(self):
            self._tools = list(registry.TOOLS)
            self._dispatch = dict(registry.DISPATCH)
            self._coercions = dict(registry.COERCIONS)
            return registry

        def __exit__(self, *exc):
            registry.TOOLS[:] = self._tools
            registry.DISPATCH.clear()
            registry.DISPATCH.update(self._dispatch)
            registry.COERCIONS.clear()
            registry.COERCIONS.update(self._coercions)
            return False
    return _Sandbox()


def _expect_declaration_error(fn, description, **args):
    try:
        registry.register(fn, description, **args)
    except registry.DeclarationError as exc:
        return str(exc)
    raise AssertionError("%s should have been refused" % fn.__name__)


def test_declaring_an_argument_the_function_does_not_accept_is_refused():
    with _fresh():
        def widget_a(street=None, progress=None):
            return {}
        msg = _expect_declaration_error(
            widget_a, "d",
            street=registry.arg("string", "s"),
            postcode=registry.arg("string", "p"))
        assert "postcode" in msg and "rejects" in msg


def test_an_argument_the_function_accepts_but_never_declares_is_refused():
    with _fresh():
        def widget_b(street=None, secret=None, progress=None):
            return {}
        msg = _expect_declaration_error(widget_b, "d",
                                        street=registry.arg("string", "s"))
        assert "secret" in msg and "no host can see" in msg


def test_a_tool_that_cannot_take_progress_is_refused():
    """A tool with no progress sink prints, and on the MCP surface stdout is
    the protocol (E3)."""
    with _fresh():
        def widget_c(street=None):
            return {}
        msg = _expect_declaration_error(widget_c, "d",
                                        street=registry.arg("string", "s"))
        assert "progress" in msg and "stdout is the protocol" in msg


def test_a_duplicate_name_is_refused():
    with _fresh():
        msg = _expect_declaration_error(tools.coverage, "a second coverage")
        assert "already registered" in msg


def test_an_unknown_json_type_is_refused():
    try:
        registry.arg("str", "a description")
    except registry.DeclarationError as exc:
        assert "JSON Schema type" in str(exc)
    else:
        raise AssertionError("'str' is not a JSON Schema type")


def test_every_shipped_declaration_matches_its_function():
    """The check register() applies, re-run over the ten as they stand — so the
    guarantee is asserted, not merely trusted to have held at import."""
    for entry in tools.TOOLS:
        fn = tools.DISPATCH[entry["name"]]
        params = set(inspect.signature(fn).parameters)
        assert registry.PROGRESS in params, entry["name"]
        assert set(entry["inputSchema"]["properties"]) == params - {registry.PROGRESS}, \
            entry["name"]


# ---------------------------------------------------------------------------
# The coercion table — the CLI stops handing tools strings.
# ---------------------------------------------------------------------------

def test_coercers():
    assert registry.as_int("2") == 2
    assert registry.as_int("1,350,000") == 1350000
    assert registry.as_float("7.5") == 7.5
    assert registry.as_bool("yes") is True and registry.as_bool("off") is False
    assert registry.as_list("a, b ,c") == ["a", "b", "c"]
    assert registry.as_list("a,,b") == ["a", "b"]
    assert registry.as_json('{"k": 1}') == {"k": 1}
    for bad in ("two", "", "3.5"):
        try:
            registry.as_int(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("as_int(%r) should raise" % bad)
    try:
        registry.as_bool("maybe")
    except ValueError:
        pass
    else:
        raise AssertionError("as_bool('maybe') should raise")


def test_a_type_implies_its_coercer_and_a_declaration_may_override():
    with _fresh():
        def widget_d(beds=None, ratio=None, verbose=None, towns=None,
                     street=None, progress=None):
            return {"beds": beds, "ratio": ratio, "verbose": verbose,
                    "towns": towns, "street": street}
        registry.register(
            widget_d, "d",
            beds=registry.arg("integer", "how many"),
            ratio=registry.arg("number", "how much"),
            verbose=registry.arg("boolean", "loud?"),
            towns=registry.arg("array", "which"),
            # A string argument with a parse of its own.
            street=registry.arg("string", "where", coerce=str.upper))
        got = registry.coerce_cli_args("widget_d", {
            "beds": "3", "ratio": "1.5", "verbose": "yes",
            "towns": "leeds, york", "street": "de beauvoir road"})
        assert got == {"beds": 3, "ratio": 1.5, "verbose": True,
                       "towns": ["leeds", "york"], "street": "DE BEAUVOIR ROAD"}


def test_an_undeclared_argument_passes_through_untouched():
    """A typo'd argument must still reach safe_call, which names it. Swallowing
    it here would turn a clear 'bad arguments' into a silent no-op."""
    assert registry.coerce_cli_args("price_check", {"stret": "x"}) == {"stret": "x"}


def test_a_bad_value_names_the_argument():
    with _fresh():
        def widget_e(beds=None, progress=None):
            return {}
        registry.register(widget_e, "d", beds=registry.arg("integer", "how many"))
        try:
            registry.coerce_cli_args("widget_e", {"beds": "two"})
        except ValueError as exc:
            assert "beds=two" in str(exc)
        else:
            raise AssertionError("a bad integer should raise")


def test_the_ten_shipped_tools_declare_no_coercions():
    """Every v0 argument is a string or an object, so nothing is coerced and the
    CLI's behaviour is unchanged by S3. The mechanism is tested above on a
    declared-for-the-test tool; this pins that no SHIPPED tool silently gained a
    conversion. ``situate`` is deliberately outside the set: it is the first
    tool to take numbers, and reading "£320,000" at the CLI is the mechanism
    working, not a shipped tool drifting."""
    assert set(registry.COERCIONS) & _V0_TOOLS == set()


# ---------------------------------------------------------------------------
# The CLI still behaves exactly as its contract says.
# ---------------------------------------------------------------------------

def _cli(*argv):
    proc = subprocess.run([sys.executable, "-c",
                           "import sys; sys.argv=['gaff']+%r; "
                           "from gaff_engine.tools import cli_main; "
                           "sys.exit(cli_main())" % list(argv)],
                          cwd=_ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_help_lists_every_tool():
    rc, out, _err = _cli("--help")
    assert rc == 0
    for name in tools.DISPATCH:
        assert name in out, name


def test_cli_exit_codes_are_unchanged():
    assert _cli("nope")[0] == 2                       # unknown tool
    assert _cli("coverage", "bare")[0] == 2           # not key=value
    # 0 answered / 1 ran into the data. Either is fine here and depends on what
    # is warmed; the contract this pins is that neither is 2, which means "fix
    # your command".
    assert _cli("coverage")[0] != 2


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
    print("\n%s" % ("all registry tests passed" if not failures
                    else "%d FAILURES" % failures))
    sys.exit(1 if failures else 0)
