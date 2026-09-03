"""U7 — the Forensics scorer (03-engine §5.5). The photo + floorplan vision read.

The fourth scorer, and the odd one out: it does not feed the Mix. It is a
**shared pre-compute** — the expensive vision read that runs ONCE per
``listingKey`` (Person-independent) and every Person's taste/value read then
consumes cheaply (the two-speed unit economics, 00-frame). It produces the
``forensics@1`` payload and the **viewing flags** — the protective half of the
Taste scorer's dual output (predicted reaction *and* the things you would only
discover at a viewing: the 2.46 m-wide everything, the four-ensuites-no-living-
room floorplan, the white-box flip). Prediction calibrates to the Person's eyes;
flags protect the Person's decision. The two never merge (§5.1).

Architecture — the same deterministic-pipeline-over-a-pluggable-model shape as
U6: the **vision judgement is an LLM read** behind a :class:`ForensicsModel`
(a live model prompts a vision LLM over the images + floorplans; the
:func:`canonical_model` used by the engine + tests replays the recorded De
Beauvoir read, so the build stays deterministic). Everything after the read is
pure Python: the flag derivation and the fatal-signal feed into taste.

What the read extracts into ``forensics@1`` (§5.5): ``roomWidthsM``,
``walkThroughBedroom``, ``hmoTells``, ``cheapFlipSignals``, ``aspect``,
``ceilingHeightCue``, ``floorPosition``.

What it feeds (``source:"forensics"`` flags, into ``score.result.flags``):
* ``cheap_careless_spec`` — **fatal**: the one Forensics output that can kill a
  listing. It routes to the §5.1 fatal anti-signal (forces taste ≤ 2.0), which at
  the Buy mix drops the composite below ``threshold.show`` — a listing that clears
  the declared gates but reveals a cheap flip in its photos is quietly killed.
* ``lower_ground_light`` / ``north_facing`` — ``kind:"viewing"``, severity
  ``watch`` (check daylight / orientation at the viewing).
* ``hmo_history`` — ``kind:"viewing"``, severity ``watch`` (second kitchen / multiple
  ensuites → check HMO history).

Cache-once contract: :func:`forensics_for` memoises by ``listingKey`` (a new
``imageSetHash`` on a relist is a different read — P9's invalidation key), so the
vision cost is paid once per listing, not once per Person.

Worked (De Beauvoir): ``aspect:"south-west (rear)"``, ``ceilingHeightCue:"generous
(bay)"``, ``hmoTells:false``, ``cheapFlipSignals:[]``, ``floorPosition:"raised +
lower ground"`` → one ``lower_ground_light`` watch flag. Reproduces §5.5b's
ScoreResult flag sourced to ``forensics``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from gaff_engine.schemas import (
    Flag, FlagCode, FlagKind, FlagSeverity, Forensics,
)

# ---------------------------------------------------------------------------
# CONFIG — the flag semantics (severity/kind/text) the read feeds (§5.5). The
# nominal taste penalty for a fatal cheap-flip is intentionally SMALL: the fatal
# *cap* (taste ≤ 2.0, applied in U6 stage 4) does the killing, not the dock.
# ---------------------------------------------------------------------------

CONFIG: Dict[str, Any] = {
    "cheapFlipTastePenalty": -0.5,   # nominal; the fatal ≤2.0 cap in U6 dominates
    # keyword cues the deterministic fallback matches when a model omits a field.
    "lowerGroundCues": ("lower ground", "lower-ground", "garden level", "raised and lower"),
    "northCues": ("north-facing", "north facing", "due north"),
}

_FORENSICS = "forensics"


# ---------------------------------------------------------------------------
# Small accessors (codebase style, cf. taste._g / rules._g).
# ---------------------------------------------------------------------------

def _g(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        cur = obj
        ok = True
        for part in name.split("."):
            if cur is None:
                ok = False
                break
            if isinstance(cur, dict):
                if part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            elif hasattr(cur, part):
                cur = getattr(cur, part)
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def _text_blob(listing: Any) -> str:
    bits: List[str] = []
    desc = _g(listing, "description")
    if desc:
        bits.append(str(desc))
    kf = _g(listing, "keyFeatures")
    if kf:
        bits.extend(str(x) for x in kf)
    return " ".join(bits).lower()


def _listing_key(listing: Any) -> str:
    return str(_g(listing, "listingKey", "id", default=""))


# ---------------------------------------------------------------------------
# The model boundary — the vision read is a Forensics payload; a ForensicsModel
# produces one per Listing. Live models prompt a vision LLM; RecordedModel
# replays a recording (canonical De Beauvoir) so the engine build is deterministic.
# ---------------------------------------------------------------------------

class RecordedForensicsModel:
    """A deterministic :class:`ForensicsModel`: replays stored reads keyed by
    ``listingKey``. Unknown keys fall back to a *conservative* empty read (no
    cheap-flip, no HMO, floor position inferred from the listing text) so an
    unrecognised listing is never silently killed or silently cleared."""

    def __init__(self, reads: Optional[Dict[str, Forensics]] = None):
        self._reads = dict(reads or {})

    def __call__(self, listing: Any) -> Forensics:
        key = _listing_key(listing)
        if key in self._reads:
            return self._reads[key]
        return _conservative_read(listing)


def _conservative_read(listing: Any) -> Forensics:
    """A no-vision fallback: infer only what the listing TEXT admits (floor
    position from keyFeatures/description); assume no cheap-flip / no HMO (never
    invent a kill). A live vision model replaces this for real coverage."""
    blob = _text_blob(listing)
    floor = None
    if any(c in blob for c in CONFIG["lowerGroundCues"]):
        floor = "lower ground (from listing text)"
    aspect = "north-facing (from listing text)" if any(c in blob for c in CONFIG["northCues"]) else None
    return Forensics(
        id="forensics_%s" % (_listing_key(listing) or "unknown"),
        listingKey=_listing_key(listing),
        roomWidthsM=None, walkThroughBedroom=None, hmoTells=False,
        cheapFlipSignals=[], aspect=aspect, ceilingHeightCue=None,
        floorPosition=floor, imageSetHash=None,
    )


# ---------------------------------------------------------------------------
# Cache-once per listingKey (the unit-economics contract, §5.5).
# ---------------------------------------------------------------------------

def forensics_for(listing: Any, model: Any, *,
                  cache: Optional[Dict[str, Forensics]] = None) -> Forensics:
    """The Forensics payload for ``listing`` via ``model`` — memoised by
    ``listingKey`` (Person-independent, once per listing). ``cache`` is an
    injectable dict so a caller can share one across a scoring batch; omit it and
    a per-call read is returned (still deterministic for a RecordedModel)."""
    key = _listing_key(listing)
    if cache is not None and key in cache:
        return cache[key]
    f = model(listing)
    if cache is not None:
        cache[key] = f
    return f


# ---------------------------------------------------------------------------
# Flag derivation (pure) — the viewing flags + the fatal cheap-flip.
# ---------------------------------------------------------------------------

def _is_lower_ground(forensics: Any, listing: Any) -> bool:
    fp = str(_g(forensics, "floorPosition", default="") or "").lower()
    if any(c in fp for c in CONFIG["lowerGroundCues"]) or "lower" in fp:
        return True
    return any(c in _text_blob(listing) for c in CONFIG["lowerGroundCues"])


def _is_north(forensics: Any) -> bool:
    asp = str(_g(forensics, "aspect", default="") or "").lower()
    return any(c in asp for c in CONFIG["northCues"]) or "north" in asp


def forensics_flags(forensics: Any, listing: Any = None) -> List[Flag]:
    """Derive the ``source:"forensics"`` flags from the payload (§5.5). Order:
    the fatal cheap-flip first (the kill), then the viewing watch-flags."""
    flags: List[Flag] = []

    signals = list(_g(forensics, "cheapFlipSignals", default=[]) or [])
    if signals:
        flags.append(Flag(
            code=FlagCode.CHEAP_CARELESS_SPEC, severity=FlagSeverity.SERIOUS,
            kind=FlagKind.LISTING, source=_FORENSICS,
            text="Cheap/careless spec in the photos (%s) — a fatal kill, not a "
                 "cosmetic dock." % ", ".join(signals)))

    if _is_lower_ground(forensics, listing):
        flags.append(Flag(
            code=FlagCode.LOWER_GROUND_LIGHT, severity=FlagSeverity.WATCH,
            kind=FlagKind.VIEWING, source=_FORENSICS,
            text="Lower-ground floor — check daylight in the rear rooms at viewing."))

    if _is_north(forensics):
        flags.append(Flag(
            code=FlagCode.NORTH_FACING, severity=FlagSeverity.WATCH,
            kind=FlagKind.VIEWING, source=_FORENSICS,
            text="North-facing aspect — check how much direct sun the main rooms get."))

    if bool(_g(forensics, "hmoTells", default=False)):
        flags.append(Flag(
            code=FlagCode.HMO_HISTORY, severity=FlagSeverity.WATCH,
            kind=FlagKind.VIEWING, source=_FORENSICS,
            text="HMO tells on the floorplan (second kitchen / multiple ensuites) "
                 "— check the HMO history and licensing."))

    return flags


def fatal_anti_signals(forensics: Any) -> List[Tuple[str, float, bool]]:
    """The fatal anti-signal(s) Forensics feeds the Taste scorer (U6 stage 4).
    A non-empty ``cheapFlipSignals`` yields one ``(signal, penalty, fatal=True)``
    tuple; the small penalty is nominal — the fatal ≤2.0 cap does the killing."""
    signals = list(_g(forensics, "cheapFlipSignals", default=[]) or [])
    if not signals:
        return []
    return [("cheap/careless spec (forensics)", CONFIG["cheapFlipTastePenalty"], True)]


def layout_docks(forensics: Any) -> List[str]:
    """Layout kills the read surfaces for the Taste ``width_proportion_flow`` /
    ``separate_living_room`` docks (§5.5): a walk-through bedroom or a skinny
    room. Returned as human-readable notes the caller may route into taste."""
    notes: List[str] = []
    if bool(_g(forensics, "walkThroughBedroom", default=False)):
        notes.append("walk-through bedroom (floorplan)")
    widths = list(_g(forensics, "roomWidthsM", default=[]) or [])
    if widths and min(widths) < 2.6:
        notes.append("skinny room at %.2f m (floorplan)" % min(widths))
    return notes


# ---------------------------------------------------------------------------
# The canonical De Beauvoir recording — the read that reproduces §5.5's worked
# example + the golden ScoreResult's forensics-sourced flag. This is what the
# engine + the U7 tests inject.
# ---------------------------------------------------------------------------

def canonical_model() -> RecordedForensicsModel:
    """The :class:`RecordedForensicsModel` the engine + tests use — replays the
    golden De Beauvoir forensics read (keyed by its listingKey)."""
    from gaff_engine.fixtures.de_beauvoir import GOLDEN_FORENSICS
    return RecordedForensicsModel({GOLDEN_FORENSICS.listingKey: GOLDEN_FORENSICS})


__all__ = [
    "CONFIG", "RecordedForensicsModel", "forensics_for", "forensics_flags",
    "fatal_anti_signals", "layout_docks", "canonical_model",
]
