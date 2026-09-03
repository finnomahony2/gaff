"""The M0 stub engine — the deliberate placeholder M1 replaces (U2 / M0).

The walking skeleton proves the *pipe* — inputs → engine → serialized score →
rendered verdict card — before any real scoring exists. So the engine seam is
present and honest, but hollow: :func:`stub_score` carries the exact signature the
real engine will use, ignores its three inputs, and returns the golden De Beauvoir
``score.result@1`` verbatim.

This is intentional, not a shortcut to hide: M0's contract is "the plumbing runs
end to end and the card renders a real ``ScoreResult``"; M1's contract is "swap
the fixture for the computed result behind this same signature." Nothing else in
the pipe (serialize, build, render) needs to change when that swap happens — which
is the whole point of pinning the signature now.
"""

from __future__ import annotations

from gaff_engine.fixtures.de_beauvoir import GOLDEN_SCORE_RESULT
from gaff_engine.schemas import Listing, Person, ScoreResult, Search


def stub_score(listing: Listing, person: Person, search: Search) -> ScoreResult:
    """Return the golden De Beauvoir ``ScoreResult``, ignoring the inputs.

    The signature — ``(listing, person, search) → ScoreResult`` — is the real
    engine's contract; M1 replaces the body with genuine taste/rules/value scoring
    while every caller (``build_m0.py``, the serializer, the card) stays untouched.

    ``listing``, ``person`` and ``search`` are accepted and deliberately unused so
    the seam is real at M0 (a caller must build the true inputs to call it).
    """
    del listing, person, search  # unused at M0 — the stub is input-independent.
    return GOLDEN_SCORE_RESULT


__all__ = ["stub_score"]
