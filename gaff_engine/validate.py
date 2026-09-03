"""The contract validator (U1 build plan: the "schema validator every unit's
I/O passes at build time").

``validate(obj)`` walks a dataclass instance from :mod:`gaff_engine.schemas`
and returns a list of human-readable violation strings — empty means valid.
It checks, recursively and generically off the type hints:

* required fields present — a field whose hint is *not* ``Optional[...]`` must
  not be ``None`` (missing-required);
* types — ``int``/``float``/``bool``/``str`` (with the usual ``bool`` is not
  ``int`` care, and ``int`` accepted where ``float`` is wanted), ``List[...]``
  elements, ``Dict[...]`` keys/values, and nested dataclasses;
* enum membership — the value must be a member of the annotated Enum (a valid
  raw ``.value`` string is also accepted).

It is deliberately schema-agnostic: it reads the dataclass' own type hints, so
it stays correct as the schemas evolve. This is Principle 4's "CONTRACTS -> a
schema validator" from docs/build-plan.md.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, List, Union, get_args, get_origin, get_type_hints


def validate(obj: Any) -> List[str]:
    """Validate a dataclass instance against its schema. Empty list == valid."""
    return _validate_dataclass(obj, "")


def _validate_dataclass(obj: Any, path: str) -> List[str]:
    where = path or "<root>"
    if obj is None:
        return ["%s: missing required object" % where]
    if not is_dataclass(obj) or isinstance(obj, type):
        return ["%s: expected a dataclass instance, got %s" % (where, type(obj).__name__)]
    try:
        hints = get_type_hints(type(obj))
    except Exception:  # pragma: no cover - resolution fallback
        hints = {f.name: Any for f in fields(obj)}
    # Null-on-empty-state (P1 / A2): an object that declares an empty state via a
    # ``needs_data`` tag deliberately carries no data — a NEEDS_DATA value verdict has
    # no fair estimate, delta, band or position. Its otherwise-required fields may be
    # null; any field that IS set is still fully type-checked.
    tag = getattr(obj, "tag", None)
    empty_state = str(getattr(tag, "value", tag)) == "needs_data"
    violations: List[str] = []
    for f in fields(obj):
        ftype = hints.get(f.name, Any)
        value = getattr(obj, f.name)
        if empty_state and value is None:
            continue
        fpath = "%s.%s" % (path, f.name) if path else f.name
        violations.extend(_check(value, ftype, fpath))
    return violations


def _check(value: Any, ftype: Any, path: str) -> List[str]:
    if ftype is Any:
        return []

    origin = get_origin(ftype)
    args = get_args(ftype)

    # Optional[...] / Union[...] — accept None only when NoneType is a member.
    if origin is Union:
        allows_none = type(None) in args
        arms = [a for a in args if a is not type(None)]
        if value is None:
            return [] if allows_none else ["%s: missing required field" % path]
        best: List[str] = None  # type: ignore[assignment]
        for arm in arms:
            arm_violations = _check(value, arm, path)
            if not arm_violations:
                return []
            if best is None or len(arm_violations) < len(best):
                best = arm_violations
        return best if best is not None else ["%s: no matching type" % path]

    # A required (non-Optional) field that is absent.
    if value is None:
        return ["%s: missing required field" % path]

    # List[...] — check every element.
    if origin is list:
        if not isinstance(value, list):
            return ["%s: expected list, got %s" % (path, type(value).__name__)]
        elem_t = args[0] if args else Any
        out: List[str] = []
        for i, el in enumerate(value):
            out.extend(_check(el, elem_t, "%s[%d]" % (path, i)))
        return out

    # Dict[...] — check keys and values when parameterised.
    if origin is dict:
        if not isinstance(value, dict):
            return ["%s: expected dict, got %s" % (path, type(value).__name__)]
        out = []
        if len(args) == 2:
            kt, vt = args
            for k, v in value.items():
                out.extend(_check(k, kt, "%s.<key>" % path))
                out.extend(_check(v, vt, "%s[%r]" % (path, k)))
        return out

    # Enum — instance, or a valid raw value string.
    if isinstance(ftype, type) and issubclass(ftype, Enum):
        if isinstance(value, ftype):
            return []
        valid = {e.value for e in ftype}
        if value in valid:
            return []
        return ["%s: invalid %s %r (allowed: %s)" % (
            path, ftype.__name__, value, sorted(valid))]

    # Nested dataclass — recurse.
    if is_dataclass(ftype):
        return _validate_dataclass(value, path)

    # Primitives (bool is not int; int is accepted where float is wanted).
    if ftype is bool:
        return [] if isinstance(value, bool) else \
            ["%s: expected bool, got %s" % (path, type(value).__name__)]
    if ftype is int:
        return [] if (isinstance(value, int) and not isinstance(value, bool)) else \
            ["%s: expected int, got %s" % (path, type(value).__name__)]
    if ftype is float:
        return [] if (isinstance(value, (int, float)) and not isinstance(value, bool)) else \
            ["%s: expected float, got %s" % (path, type(value).__name__)]
    if ftype is str:
        return [] if isinstance(value, str) else \
            ["%s: expected str, got %s" % (path, type(value).__name__)]

    # Any other concrete type — best-effort isinstance.
    if isinstance(ftype, type):
        return [] if isinstance(value, ftype) else \
            ["%s: expected %s, got %s" % (path, ftype.__name__, type(value).__name__)]
    return []
