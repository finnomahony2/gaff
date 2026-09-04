"""S3 — the tool registry: one declaration per tool, three things generated.

Before this module a tool was added in two places that had to agree by hand: a
function, and a hand-written entry in a ``TOOLS`` list literal two hundred lines
away. Nothing checked that the entry's argument names matched the function's, so
a declared-but-unaccepted argument would advertise fine to a host and fail at
call time; and every new tool meant remembering the second half.

One :func:`register` call now generates all three surfaces:

* :data:`TOOLS` — the MCP manifest a host reads from ``tools/list``;
* :data:`DISPATCH` — name to callable, for both surfaces;
* :data:`COERCIONS` — argument to converter, so the **CLI** stops handing ints,
  floats, bools and comma lists to tools as strings. (CLI only, deliberately:
  an MCP host already sends typed JSON, and coercing there would change the
  behaviour of the ten shipped tools rather than refactor it.)

Not a framework. An argument is a plain dict of ``type`` / ``description`` /
``required`` / optional ``coerce`` and ``enum``; :func:`arg` builds one.

The identities matter
---------------------
``TOOLS`` is a mutable list and ``DISPATCH`` a mutable dict, re-exported by
``gaff_engine.tools`` and by the ``gaff_tools`` shim as *the same objects*. The
surface tests monkeypatch them in place (``gt.TOOLS.append(...)``,
``gt.DISPATCH['noisy'] = ...``), so these must never be rebound or replaced with
copies.

What register checks, at import time
------------------------------------
A declaration that disagrees with its function is a bug that used to surface as a
runtime ``TypeError`` in front of a user. Registration now fails loudly at import
instead: the name must be free, every declared argument must be a parameter the
function actually accepts, every parameter the function accepts (bar
``progress``) must be declared, and the function must take ``progress``, because
a tool that cannot be handed a progress sink will print — and on the MCP surface
stdout *is* the protocol (E3).
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable, Dict, List, Optional

#: The MCP manifest. A list, mutated in place — never rebound (see the docstring).
TOOLS: List[Dict[str, Any]] = []

#: Tool name -> the callable. A dict, mutated in place.
DISPATCH: Dict[str, Callable] = {}

#: Tool name -> {argument name: converter}. Applied by ``tools.cli_main`` only.
COERCIONS: Dict[str, Dict[str, Callable]] = {}

#: The JSON Schema types a declaration may use.
TYPES = ("string", "object", "array", "integer", "number", "boolean")

#: The progress sink every tool takes. Not an argument a host may pass: the
#: surface binds it (stderr for the CLI, a no-op for MCP).
PROGRESS = "progress"


class DeclarationError(Exception):
    """A tool declaration disagrees with the function it declares."""


# ---------------------------------------------------------------------------
# The CLI coercers. Every CLI argument arrives as a string; these turn the
# stated text into the type the tool signature means. A declaration may pass
# any callable, so a money argument can take a richer parser of its own.
# ---------------------------------------------------------------------------

def as_int(raw: Any) -> int:
    """``"2"`` -> ``2``. Commas allowed, because people type them."""
    if isinstance(raw, bool):                       # bool is an int subclass
        raise ValueError("expected a whole number, got a boolean")
    if isinstance(raw, int):
        return raw
    text = str(raw).strip().replace(",", "").replace("_", "")
    try:
        return int(text)
    except ValueError:
        raise ValueError("expected a whole number, got %r" % raw)


def as_float(raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError("expected a number, got a boolean")
    try:
        return float(str(raw).strip().replace(",", ""))
    except ValueError:
        raise ValueError("expected a number, got %r" % raw)


def as_bool(raw: Any) -> bool:
    """The words a person types at a shell, not just ``"True"``."""
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "yes", "y", "1", "on"):
        return True
    if text in ("false", "no", "n", "0", "off", ""):
        return False
    raise ValueError("expected yes/no, got %r" % raw)


def as_list(raw: Any) -> List[str]:
    """``"a, b ,c"`` -> ``["a", "b", "c"]``. Empty pieces dropped, order kept."""
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    return [piece.strip() for piece in str(raw).split(",") if piece.strip()]


def as_json(raw: Any) -> Any:
    """A JSON object or array typed at the shell. Kept for declarations that
    want it explicitly; the object-taking tools parse their own (``_as_dict``)
    so their error messages can name the argument."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("expected JSON: %s" % exc.msg)


#: The converter a bare type implies at the CLI, when a declaration names none.
DEFAULT_COERCE = {"integer": as_int, "number": as_float, "boolean": as_bool,
                  "array": as_list}


# ---------------------------------------------------------------------------
# The declaration.
# ---------------------------------------------------------------------------

def arg(type: str, description: str, required: bool = False,
        coerce: Optional[Callable] = None,
        enum: Optional[List[str]] = None) -> Dict[str, Any]:
    """One argument's declaration.

    ``coerce`` overrides the converter :data:`DEFAULT_COERCE` implies for the
    type; pass it to give a ``string`` argument a parse of its own, or to give
    a number a richer one than :func:`as_int`.
    """
    if type not in TYPES:
        raise DeclarationError("%r is not a JSON Schema type. Known: %s"
                               % (type, ", ".join(TYPES)))
    return {"type": type, "description": description, "required": bool(required),
            "coerce": coerce, "enum": list(enum) if enum else None}


def _schema(args: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The declaration -> the ``inputSchema`` a host reads.

    Emits ``type``, then ``enum`` when present, then ``description`` — the key
    order the hand-written manifest already used, so a refactor produces the
    same bytes rather than a diff no host cares about but every reviewer has to
    read past.
    """
    properties = {}
    for name, spec in args.items():
        if not isinstance(spec, dict) or "type" not in spec or "description" not in spec:
            raise DeclarationError(
                "argument %r must be built by arg(); got %r" % (name, spec))
        prop = {"type": spec["type"]}
        if spec.get("enum"):
            prop["enum"] = spec["enum"]
        prop["description"] = spec["description"]
        properties[name] = prop
    return {"type": "object", "properties": properties,
            "required": [n for n, s in args.items() if s.get("required")]}


def _check(name: str, fn: Callable, args: Dict[str, Dict[str, Any]]) -> None:
    """Refuse a declaration that disagrees with its function. See the docstring."""
    if name in DISPATCH:
        raise DeclarationError("a tool named %r is already registered" % name)
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):                 # pragma: no cover
        return                                      # not introspectable; trust it
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return                                      # **kwargs accepts anything
    if PROGRESS not in params:
        raise DeclarationError(
            "%s must take a '%s' argument: a tool that cannot be handed a "
            "progress sink will print, and on the MCP surface stdout is the "
            "protocol" % (name, PROGRESS))
    accepted = {p for p in params if p != PROGRESS}
    undeclared = sorted(accepted - set(args))
    if undeclared:
        raise DeclarationError(
            "%s accepts %s but does not declare %s — an argument no host can "
            "see" % (name, ", ".join(sorted(accepted)), ", ".join(undeclared)))
    unaccepted = sorted(set(args) - accepted)
    if unaccepted:
        raise DeclarationError(
            "%s declares %s but does not accept %s — a host would be told to "
            "pass an argument the call rejects"
            % (name, ", ".join(sorted(args)), ", ".join(unaccepted)))


def register(fn: Callable, description: str, /, **args: Dict[str, Any]) -> Callable:
    """Declare one tool: append to :data:`TOOLS`, fill :data:`DISPATCH`, and
    record its CLI coercions. Returns ``fn``, so it also reads as a decorator.

    The name is the function's own. Arguments are keyword arguments in the order
    a host should see them (``**kwargs`` keeps declaration order, PEP 468), each
    built by :func:`arg`. ``fn`` and ``description`` are positional-only, so an
    argument may be called ``fn`` or ``description`` without colliding.
    """
    name = fn.__name__
    _check(name, fn, args)
    TOOLS.append({"name": name, "description": description,
                  "inputSchema": _schema(args)})
    DISPATCH[name] = fn
    coercions = {}
    for arg_name, spec in args.items():
        converter = spec.get("coerce") or DEFAULT_COERCE.get(spec["type"])
        if converter is not None:
            coercions[arg_name] = converter
    if coercions:
        COERCIONS[name] = coercions
    return fn


def coerce_cli_args(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Apply one tool's CLI coercions to the parsed ``key=value`` arguments.

    Returns a new dict; an argument with no declared converter passes through as
    the string it arrived as. A bad value raises ``ValueError`` naming the
    argument, which the CLI turns into its exit-code-2 "fix your command".
    """
    coercions = COERCIONS.get(name) or {}
    out = {}
    for key, value in args.items():
        converter = coercions.get(key)
        if converter is None:
            out[key] = value
            continue
        try:
            out[key] = converter(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s=%s: %s" % (key, value, exc))
    return out


def tool(name: str) -> Optional[Dict[str, Any]]:
    """One tool's manifest entry by name, or ``None``."""
    for entry in TOOLS:
        if entry["name"] == name:
            return entry
    return None


__all__ = ["TOOLS", "DISPATCH", "COERCIONS", "TYPES", "DeclarationError",
           "arg", "register", "coerce_cli_args", "tool",
           "as_int", "as_float", "as_bool", "as_list", "as_json"]
