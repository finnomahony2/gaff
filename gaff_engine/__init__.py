"""Gaff engine — the deterministic core (U1 / Milestone M0, approach A).

The tested-library heart of the Gaff "Buy slice" walking skeleton: the data
contracts of 01-domain, a generic schema validator, and the pure composite
math of 03-engine — with the golden De Beauvoir fixture as the quality oracle.

Public surface:
    schemas    — dataclasses + enums for the core contracts (01-domain §5)
    validate   — validate(obj) -> list[str] of contract violations ([] == valid)
    composite  — taste_score(...) and composite(...) pure deterministic functions
    engine     — score(listing, person, search) -> ScoreResult (the real M1 engine)
    fixtures   — the golden De Beauvoir objects
"""

from gaff_engine.composite import composite, taste_score
from gaff_engine.engine import score
from gaff_engine.validate import validate

__all__ = ["composite", "taste_score", "validate", "score", "schemas", "fixtures"]

__version__ = "0.3.0"  # M1 complete — three live scorers (taste U6 + value U3 + rules U4)
