"""U-homepage — the P7 shell front door + routing (07-shell.md §5.1-5.2).

The door a stranger walks through first, and the no-dead-end router behind every
view. Two deterministic pieces:

* **`resolve_route()`** (§5.1) — the URL grammar `#/<view>[/<arg>]` resolved to a
  live view, with the invariant that *no hash ever yields a blank or an error
  view; the worst case is the mode's home*. Anonymous (pre-signup) routing has no
  mode, so it renders the mode-less `homepage` view for any hash.
* **`assemble_homepage()`** (§5.2) — the `homepage.spec@1`: it must *perform* the
  product (swipe real homes → an instant taste-read), not explain it. The
  taste-read is composed from a real anonymous P4 session's `taste.uncertainty@1`
  (the two highest-weight *resolved* axes + any anti-signal), and stays honest:
  if three swipes were too sparse to read, it says so rather than inventing one
  (the P3/P4 honesty contract holding even in the marketing surface).

It renders and routes; it computes no score and mutates no Person (that is P3/P4,
which the demo *embeds*). Pure + deterministic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from gaff_engine.schemas import (
    FrontDoor, HomepageSpec, Mode, NavModel, Provenance, ProvenanceSource, Ref, Route,
)
from gaff_engine.swipe import AXES

# ---------------------------------------------------------------------------
# §5.1 — the view registry + the per-Mode nav.model@1 (Buy authoritative; the
# other three modes at contract level so resolve_route's `allowed` set is
# well-defined and the no-dead-end fuzz runs in every mode).
# ---------------------------------------------------------------------------

NAV_MODELS: Dict[str, NavModel] = {
    "buy": NavModel(mode=Mode.BUY, home="feed",
                    primary=["feed", "shortlist", "map", "taste"],
                    secondary=["areas", "viewing", "playbook", "game", "start", "method"],
                    overlays=["settings", "fork"]),
    "rent": NavModel(mode=Mode.RENT, home="feed",
                     primary=["feed", "shortlist", "board", "map"],
                     secondary=["taste", "viewing", "method", "game", "start"],
                     overlays=["settings", "fork"]),
    "invest": NavModel(mode=Mode.INVEST, home="deals",
                       primary=["deals", "map", "taste"],
                       secondary=["method", "game", "start"],
                       overlays=["settings", "fork"]),
    "dream": NavModel(mode=Mode.DREAM, home="collection",
                      primary=["collection", "feed", "map", "taste"],
                      secondary=["method", "start"],
                      overlays=["settings", "fork"]),
}


def nav_model(mode: Any) -> NavModel:
    return NAV_MODELS[getattr(mode, "value", mode)]


def _parse_hash(raw_hash: str) -> Tuple[str, Optional[str]]:
    """`#/<view>[/<arg>]` → (view, arg). A bare/empty hash → ("", None)."""
    s = (raw_hash or "").lstrip("#").lstrip("/")
    if not s:
        return "", None
    parts = s.split("/")
    return parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)


def resolve_route(raw_hash: str, mode: Optional[Any] = None, *,
                  listing_ids: Tuple[str, ...] = (), sub_ids: Tuple[str, ...] = ()) -> Route:
    """The no-dead-end resolver (§5.1). Anonymous (mode is None) → the mode-less
    `homepage` view for any hash. In-app, an unknown / mode-invalid / stale-arg
    route falls to the mode's home — never a blank or a 404."""
    if mode is None:
        # Pre-auth branch: no mode to resolve against; homepage owns the root.
        return Route(view="homepage", arg=None, raw=raw_hash or "#/")

    nm = nav_model(mode)
    view, arg = _parse_hash(raw_hash)
    home = Route(view=nm.home, arg=None, raw=raw_hash or ("#/" + nm.home))
    if not view:
        return Route(view=nm.home, arg=None, raw=raw_hash or ("#/" + nm.home))

    # `fork` + `listing` are routable; `settings` is NOT (it opens over a view).
    allowed = set(nm.primary) | set(nm.secondary) | {"fork", "listing"}
    if view not in allowed:
        return home
    if view == "listing" and (arg is None or arg not in listing_ids):
        return home            # stale deep-link → home, never a broken listing page
    if view == "fork" and (arg is None or arg not in sub_ids):
        return home
    return Route(view=view, arg=arg, raw=raw_hash)


# ---------------------------------------------------------------------------
# §5.2 — the homepage / front door (homepage.spec@1).
# ---------------------------------------------------------------------------

_EQUATION = {
    "equation": "Everyone can see every home. No one can see their own.",
    "sub": ("82 listings tick every filter. About ten feel like home. "
            "Gaff learns which ten — and tells you the truth about them."),
}

# The four honest doors, one per Mode (§5.2 table). Buy is complete; the other
# three are honest and clickable but route into their mode's onboarding stub.
_FRONT_DOORS: List[FrontDoor] = [
    FrontDoor(mode=Mode.BUY, label="I'm buying",
              promise=("The biggest decision of your life, de-risked — is it a good buy, "
                       "and what's wrong that the photos hide?"),
              leadWith="Value Verdict + risk flags", cta="Start a buy search"),
    FrontDoor(mode=Mode.RENT, label="I'm renting",
              promise=("The six-week sprint, made to hurt less — the places you'd actually "
                       "love, ranked."),
              leadWith="affordability + commute", cta="Start a rent search"),
    FrontDoor(mode=Mode.INVEST, label="I'm investing",
              promise=("Forensic listing-reading a spreadsheet can't do — yield, refurb "
                       "and the walk-aways."),
              leadWith="the yield deal-table", cta="Start an invest search"),
    FrontDoor(mode=Mode.DREAM, label="I'm just dreaming",
              promise=("No budget, no clock — browse the ones you'd never filter to, and "
                       "train your eye for the day it's real."),
              leadWith="taste + imagery", cta="Start dreaming"),
]

_AXIS_LABEL = {
    "light_and_volume": "light and volume", "outdoor_space": "outdoor space",
    "character_bones": "character", "width_proportion_flow": "breadth and flow",
    "street_scene": "the street", "raw_size_threshold": "real size",
    "design_finish": "finish", "station_proximity": "the commute",
}


def _resolved_highs(uncertainty: Any, weights: Dict[str, float]) -> List[str]:
    """The observed axes that lean clearly high, ranked by importance × strength —
    the drivers of the 'you go for …' read. An axis with no observation, or one
    sitting near the neutral prior, does not qualify (no invented read)."""
    rows = []
    for axis in AXES:
        b = uncertainty.axes[axis]
        if b.nObs > 0 and b.mean >= 6.5:
            rows.append((axis, float(weights.get(axis, 0.0)) * (b.mean - 5.0)))
    rows.sort(key=lambda r: -r[1])
    return [a for a, _ in rows]


def _anti_hit(uncertainty: Any) -> Optional[str]:
    for sig, b in (uncertainty.antiSignals or {}).items():
        if b.leaning == "dislike" and (b.confirmed or b.mentions >= 1):
            return sig
    return None


def taste_read(uncertainty: Any, person: Any, *,
               prediction: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compose the instant 'feel understood' read (§5.2). Honest: a sparse read
    says so ('give me one more — I'm not sure yet') rather than faking one."""
    weights = getattr(getattr(person, "taste", None), "weights", None) or {}
    highs = _resolved_highs(uncertainty, weights)
    anti = _anti_hit(uncertainty)
    clarity = uncertainty.overall.clarity0to1 if uncertainty.overall else 0.0

    if not highs and anti is None:
        named = None
        sure = False
    else:
        parts = [_AXIS_LABEL[a] for a in highs[:2]]
        sentence = ("You go for %s" % " and ".join(parts)) if parts else "You"
        if anti is not None:
            sentence += "; you bounced off the %s" % _anti_label(anti)
        named = sentence + "."
        sure = True

    read: Dict[str, Any] = {
        "clarityJump": {"from": 0.0, "to": clarity,
                        "copy": "The clarity meter moved from 0 to %d%% — a real, weight-aware read." % round(clarity * 100)},
        "namedRead": named if sure else "Give me one more — I'm not sure yet.",
        "sure": sure,
    }
    if prediction is not None:
        read["onePrediction"] = prediction
    return read


def _anti_label(signal: str) -> str:
    s = signal.lower()
    if "cheap" in s or "careless" in s or "flip" in s:
        return "flip"
    if "marble" in s:
        return "marble"
    if "carpet" in s:
        return "carpets"
    if "galley" in s:
        return "galley kitchen"
    return signal


def assemble_homepage(uncertainty: Any, person: Any, *,
                      demo_refs: List[Ref], prediction: Optional[Dict[str, Any]] = None,
                      n_cards: int = 3) -> HomepageSpec:
    """Assemble the homepage.spec@1 (§5.2): the equation, the live on-page swipe
    demo config (embeds the real P4 deck), the instant taste-read composed from
    the anonymous session, and the four honest front doors."""
    demo = {
        "deckSource": "elicitation.session@1 (anonymous)",
        "coldStart": "taste.twin@1 (broadest London prior; volunteered nothing yet)",
        "cards": n_cards,
        "cardPool": "real, hand-vetted demo listings (isDemo:true), one distinctive · one ordinary-nice · one polarising",
        "gestures": ["right", "left", "up"],
        "whyOnTap": True, "scoreHidden": True, "revealAfter": n_cards,
        "listingRefs": [{"id": r.id, "schemaVersion": r.schemaVersion} for r in demo_refs],
    }
    read = taste_read(uncertainty, person, prediction=prediction)
    handoff = {
        "carries": "the anonymous session's reactions become a real Person on signup",
        "chosenDoorSetsMode": True,
        "note": "if they leave, nothing is stored (privacy); the up-swipe is taste-only pre-signup (no Dream Search yet)",
    }
    return HomepageSpec(
        headline=dict(_EQUATION), demo=demo, tasteRead=read, frontDoors=list(_FRONT_DOORS),
        handoff=handoff,
        provenance=Provenance(source=ProvenanceSource.DEMO, isDemo=True,
                              fetchedAt="2026-07-14T08:00:00Z", freshness="fresh"))


__all__ = [
    "NAV_MODELS", "nav_model", "resolve_route", "taste_read", "assemble_homepage",
]
