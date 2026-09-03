"""Deterministic JSON serialization for the schema dataclasses (U2 / M0).

The engine speaks dataclasses (:mod:`gaff_engine.schemas`); every surface that is
not Python — the ``data/m0_score.json`` artifact, the JSON embedded in
``app/m0.html`` — speaks plain JSON. This module is the one bridge, kept **pure
and deterministic** so the M0 pipe (engine → serialized score → rendered card)
has a single, testable seam.

``to_jsonable(obj)`` walks any value built from the schema vocabulary and returns
a structure of only ``dict`` / ``list`` / ``str`` / ``int`` / ``float`` / ``bool``
/ ``None`` — exactly what :func:`json.dumps` accepts with no ``default=`` hook:

* a **dataclass instance** → an ordered ``dict`` (fields in definition order, so
  the output is stable run to run); a single trailing underscore is stripped from
  each field name, which restores the one PEP 8 keyword-collision rename the
  schemas use (``ComponentInput.from_`` → ``"from"``). Other names pass verbatim.
* an **Enum** member → its ``.value`` (the schema enums mix in ``str``, so this is
  the wire string, e.g. ``ValueTag.FAIR`` → ``"fair"``). Checked *before* the
  primitive fall-through because those members are also ``str`` instances.
* a **list / tuple** → a ``list`` of converted elements.
* a **dict** → a ``dict`` with converted values (and Enum keys mapped to
  ``.value``), keys otherwise untouched.
* anything else (``str`` / ``int`` / ``float`` / ``bool`` / ``None``) → itself.

``score_result_to_json(sr)`` is the convenience wrapper the build uses: pretty,
UTF-8, field order preserved (never ``sort_keys``, so the ordered dicts win).

No I/O, no globals, no mutation of the input — deterministic by construction.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any


def to_jsonable(obj: Any) -> Any:
    """Recursively convert a schema value into a JSON-serializable structure.

    Pure and deterministic: the input is never mutated and the same input always
    yields the same output (dataclass fields emit in definition order).
    """
    # Dataclass INSTANCE (not the class object) → ordered dict of jsonable fields.
    if is_dataclass(obj) and not isinstance(obj, type):
        out = {}
        for f in fields(obj):
            key = f.name[:-1] if f.name.endswith("_") else f.name
            out[key] = to_jsonable(getattr(obj, f.name))
        return out

    # Enum (including the str-mixin schema enums) → its wire value. MUST precede
    # the primitive fall-through, since those members are also ``str`` instances.
    if isinstance(obj, Enum):
        return obj.value

    # Sequence → list of converted elements.
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]

    # Mapping → converted values; Enum keys collapse to their wire value.
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kk = k.value if isinstance(k, Enum) else k
            out[kk] = to_jsonable(v)
        return out

    # Primitive (str / int / float / bool / None) — already JSON-native.
    return obj


def score_result_to_json(sr: Any, indent: int = 2) -> str:
    """Serialize a ``ScoreResult`` (or any schema object) to a pretty JSON string.

    ``ensure_ascii=False`` keeps the £/–/· characters in the evidence and reason
    text readable; field order is preserved (no ``sort_keys``) so the output is
    byte-stable across runs.
    """
    return json.dumps(to_jsonable(sr), indent=indent, ensure_ascii=False)


__all__ = ["to_jsonable", "score_result_to_json"]
