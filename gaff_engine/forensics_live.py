"""U7-live — the LiveForensicsModel adapter (M4, 03-engine §5.5).

The forensics counterpart to :mod:`gaff_engine.taste_live`: the seam that makes
the photo + floorplan read **live on any listing**. A :class:`LiveForensicsModel`
implements the exact ``ForensicsModel`` interface the engine calls
(``(listing) -> Forensics``), but instead of replaying a recording it:

1. **builds a vision read request** from the Listing's images + floorplans +
   structured fields — :func:`build_vision_request`;
2. **calls a pluggable ``vision_fn(request) -> response``** — the vision-model
   boundary (a multimodal LLM over the images in production);
3. **parses the structured response into a** ``forensics@1`` — :func:`parse_vision_response`.

**Honest boundary (why this ships as a seam, not an executed read):** unlike
U9-live (live Land Registry/EPC calls work here) and U6-live (a text LLM read is
producible), a real forensics read needs the *actual photos*, which the Listing
carries only as portal URLs — the images are not fetchable in this offline
sandbox, and no vision model is wired. So U7-live delivers the **adapter + the
request/response contract, verified against the recorded golden**, ready for a
production vision model to drop into ``vision_fn``. The recorded
:func:`gaff_engine.forensics.canonical_model` remains the offline default.

Response contract (what ``vision_fn`` must return):
    {
      "roomWidthsM":        [<float>, ...] | null,   # floorplan scale-bar widths
      "walkThroughBedroom": <bool> | null,
      "hmoTells":           <bool>,                  # ensuites / second kitchen / locks
      "cheapFlipSignals":   ["<signal>", ...],       # grey refurb / laminate / white-box (FATAL)
      "aspect":             "<orientation>" | null,  # "south-west (rear)" / "north-facing"
      "ceilingHeightCue":   "<cue>" | null,
      "floorPosition":      "<floor>" | null         # "raised + lower ground"
    }
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from gaff_engine.forensics import _g, _listing_key
from gaff_engine.ingest import UNTRUSTED_LISTING_NOTE
from gaff_engine.schemas import Forensics


def build_vision_request(listing: Any) -> Dict[str, Any]:
    """The structured vision read request: the image + floorplan URLs the model
    looks at, plus the structured fields that frame the read. Pure + JSON-able."""
    images = [_g(m, "url") for m in (_g(listing, "images") or []) if _g(m, "url")]
    floorplans = [_g(m, "url") for m in (_g(listing, "floorplans") or []) if _g(m, "url")]
    return {
        "schemaVersion": "forensics.read.request@1",
        "listingKey": _listing_key(listing),
        "imageUrls": images,
        "floorplanUrls": floorplans,
        # Injection guard: keyFeatures text and anything readable inside the
        # images is listing-authored — data, never instructions.
        "contextNote": UNTRUSTED_LISTING_NOTE,
        "context": {
            "propertyType": str(_enum(_g(listing, "propertyType")) or ""),
            "beds": _g(listing, "beds"), "baths": _g(listing, "baths"),
            "receptions": _g(listing, "receptions"), "sqft": _g(listing, "sqft"),
            "keyFeatures": list(_g(listing, "keyFeatures") or []),
        },
    }


def _enum(v: Any) -> Any:
    return getattr(v, "value", v)


def parse_vision_response(response: Dict[str, Any], listing: Any) -> Forensics:
    """Validate + parse a vision-model response into a ``forensics@1``. Unknown
    keys are ignored; ``cheapFlipSignals`` defaults to ``[]`` (never invents a
    kill), ``hmoTells`` to ``False`` — the conservative floor (§5.5)."""
    widths = response.get("roomWidthsM")
    if widths is not None:
        widths = [float(w) for w in widths]
    return Forensics(
        id="forensics_%s" % (_listing_key(listing) or "unknown"),
        listingKey=_listing_key(listing),
        roomWidthsM=widths,
        walkThroughBedroom=response.get("walkThroughBedroom"),
        hmoTells=bool(response.get("hmoTells", False)),
        cheapFlipSignals=list(response.get("cheapFlipSignals") or []),
        aspect=response.get("aspect"),
        ceilingHeightCue=response.get("ceilingHeightCue"),
        floorPosition=response.get("floorPosition"),
        imageSetHash=response.get("imageSetHash"),
    )


class LiveForensicsModel:
    """A live ``ForensicsModel``: builds the vision request, calls ``vision_fn``,
    parses → ``forensics@1``. Drop-in for the recorded model
    (``score(..., forensics_model=LiveForensicsModel(my_vision))``).

    ``vision_fn(request) -> response`` is the vision-model boundary. Optional
    ``recorder`` freezes each ``{listingKey: response}`` into a replay."""

    def __init__(self, vision_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
                 recorder: Optional[Dict[str, Any]] = None):
        self._vision_fn = vision_fn
        self._recorder = recorder

    def __call__(self, listing: Any) -> Forensics:
        request = build_vision_request(listing)
        response = self._vision_fn(request)
        if self._recorder is not None:
            self._recorder[request["listingKey"]] = response
        return parse_vision_response(response, listing)


def replay_vision_model(recordings: Dict[str, Dict[str, Any]]) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """A deterministic ``vision_fn`` replaying recorded responses keyed by
    ``listingKey`` — the frozen form of a live vision run, for tests + the build."""
    def _fn(request: Dict[str, Any]) -> Dict[str, Any]:
        key = request.get("listingKey")
        if key not in recordings:
            raise KeyError("no recorded forensics response for %s" % key)
        return recordings[key]
    return _fn


__all__ = [
    "LiveForensicsModel", "build_vision_request", "parse_vision_response",
    "replay_vision_model",
]
