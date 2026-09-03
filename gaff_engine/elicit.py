"""U18 — minimal elicitation (04-elicitation). A few answers → a ``person@1``.

The front door's job: turn the smallest honest set of answers into a valid
:class:`Person` a Buy Search can score against — enough to *start*, not the full
Swipe deck (that's the in-app taste view, P4). The fuller path
(:func:`person_from_profile`) lifts a complete ``profile.json`` v3 into a Person;
the minimal path (:func:`person_from_answers`) takes a handful of setup answers
(household, hard constraints, a ranked taste priority list, narration tone) and
fills sensible defaults for everything the user hasn't said yet — the taste
weights come from the priority ranking, decaying down a fixed scale, with the
un-ranked axes held at a low floor.

Deterministic + pure: same answers → the same Person (a fixed ``id`` derived from
the subject name, no clock), so the M3 build is byte-idempotent.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from gaff_engine.schemas import (
    DealBreaker, Person, Privacy, ProfileMeta, TasteProfile,
)

# The eight taste axes, in the profile's canonical weight order (the default
# ranking when the user gives none) — the same axes U6 scores.
AXES = ["light_and_volume", "outdoor_space", "character_bones",
        "width_proportion_flow", "street_scene", "raw_size_threshold",
        "design_finish", "station_proximity"]

# The weight scale a ranked priority list decays down (rank 1 → 10, then the
# scale); axes the user does not rank sit at the floor. Mirrors the shape of the
# v3 weights (10, 9, 8.5, 8, 8, 6, 4, 0.5) without pinning the exact profile.
_WEIGHT_SCALE = [10.0, 9.0, 8.5, 8.0, 8.0, 6.0, 4.0]
_WEIGHT_FLOOR = 0.5


def _weights_from_priorities(priorities: Optional[List[str]]) -> Dict[str, float]:
    """Ranked axis priorities → the eight-axis weight map. Ranked axes take the
    decaying scale (rank 1 = 10); the rest sit at the floor. An empty/None list
    falls back to the canonical order (the profile default)."""
    order = [a for a in (priorities or []) if a in AXES]
    for a in AXES:                                 # append the unranked, canonical order
        if a not in order:
            order.append(a)
    weights: Dict[str, float] = {}
    for i, axis in enumerate(order):
        weights[axis] = _WEIGHT_SCALE[i] if i < len(_WEIGHT_SCALE) else _WEIGHT_FLOOR
    return {a: weights[a] for a in AXES}           # keyed in canonical axis order


def _person_id(name: str) -> str:
    return "person_" + hashlib.sha1(("gaff|" + str(name)).encode("utf-8")).hexdigest()[:24].upper()


def person_from_answers(answers: Dict[str, Any]) -> Person:
    """Build a valid :class:`Person` from the minimal setup answers:

    * ``name`` (str), ``household`` ("sharers"|"couple"|"solo"),
    * ``minBeds`` / ``minBaths`` / ``minSqft`` (ints), ``outdoorRequired`` (bool),
    * ``tastePriorities`` (ordered list of axis keys — most important first),
    * ``narrationTone`` ("plain"|"warm"|"forensic", default "plain").

    Everything unstated takes an honest default; nothing is invented that the user
    could contradict later (the profile is versioned and mutable, P4)."""
    name = answers.get("name") or "You"
    household = answers.get("household") or "sharers"
    tone = answers.get("narrationTone") or "plain"
    weights = _weights_from_priorities(answers.get("tastePriorities"))

    hard = {
        "minBeds": int(answers.get("minBeds", 2)),
        "minBaths": int(answers.get("minBaths", 2)),
        "minSqft": int(answers.get("minSqft", 900)),
        "outdoor": "required-private-preferred" if answers.get("outdoorRequired", True) else "optional",
    }

    return Person(
        id=_person_id(name),
        subject=name,
        profile=ProfileMeta(version=3, updated=answers.get("updated", "2026-07-14")),
        lifeStage={"household": household, "intent": "buy",
                   "upgradingOn": answers.get("upgradingOn") or []},
        values={"ranked": [], "narrationTone": tone},
        riskAppetite={"leaseYearsFloor": int(answers.get("leaseYearsFloor", 90)),
                      "conditionTolerance": answers.get("conditionTolerance", "cosmetic"),
                      "priceStretchPct": int(answers.get("priceStretchPct", 5)),
                      "flagSensitivity": "high"},
        universalDealBreakers=[
            DealBreaker(code="cheap_careless_spec",
                        label="Cheap/careless flip (laminate, grey landlord refurb)",
                        kind="gate", scope="universal"),
            DealBreaker(code="bad_street_scene", label="Bad street scene",
                        kind="gate", scope="universal"),
        ],
        taste=TasteProfile(
            weights=weights,
            lovesNamed=answers.get("lovesNamed") or [],
            hardConstraintsDefault=hard),
        privacy=Privacy(exportable=True, retention="user-controlled"),
    )


def person_from_profile(profile: Dict[str, Any]) -> Person:
    """The fuller path: lift a complete ``profile.json`` v3 dict → a Person
    (weights, loves, hard constraints, calibration). Used when the user has
    already built a profile (the in-app taste view), rather than the cold start."""
    weights = {str(k): float(v) for k, v in (profile.get("weights") or {}).items()}
    hc = profile.get("hard_constraints") or {}
    return Person(
        id=_person_id(profile.get("subject") or "You"),
        subject=profile.get("subject") or "You",
        profile=ProfileMeta(version=int(profile.get("version", 3)),
                            updated=profile.get("updated", "2026-07-14")),
        lifeStage={"household": "sharers", "intent": "mixed"},
        values={"ranked": [], "narrationTone": "plain"},
        riskAppetite={"leaseYearsFloor": 90, "conditionTolerance": "cosmetic",
                      "priceStretchPct": 5, "flagSensitivity": "high"},
        universalDealBreakers=[
            DealBreaker(code="cheap_careless_spec", label="Cheap/careless flip",
                        kind="gate", scope="universal"),
        ],
        taste=TasteProfile(
            weights=weights or _weights_from_priorities(None),
            lovesNamed=profile.get("taste_loves_named") or [],
            scoringNotes=profile.get("scoring_notes"),
            hardConstraintsDefault={
                "minBeds": hc.get("min_beds", 2), "minBaths": hc.get("min_baths", 2),
                "minSqft": hc.get("min_sqft", 900), "outdoor": "required-private-preferred"}),
        privacy=Privacy(exportable=True, retention="user-controlled"),
        calibration=profile.get("calibration"),
    )


__all__ = ["person_from_answers", "person_from_profile", "AXES", "_weights_from_priorities"]
