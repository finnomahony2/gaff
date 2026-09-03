"""U6-live — the LiveTasteModel adapter (M4, 03-engine §5.1).

Phase 1 scored taste through *recorded* per-listing reads (the canonical De
Beauvoir read + the demo reads). This is the seam that makes the taste read
**live on any listing**: a :class:`LiveTasteModel` implements the exact
``TasteModel`` interface the engine already calls
(``(listing, person, *, use_images) -> TasteRead``), but instead of replaying a
recording it:

1. **builds a structured read request** from the Person's taste rubric (the eight
   axis weights + ``scoringNotes``) and the Listing's evidence (description,
   keyFeatures, propertyType, sqft, receptions, newBuild) — :func:`build_request`;
2. **calls a pluggable ``model_fn(request) -> response``** — the LLM boundary. In
   production this formats the request as a prompt and calls the model; here the
   deterministic :func:`replay_model` (and the tests) inject a recorded response,
   and a live read is produced by *any* callable returning the response contract;
3. **parses the structured response into a :class:`TasteRead`** — :func:`parse_response`
   — which flows into U6's real pipeline unchanged (weighted base → named-love →
   anti-signals → caps → the recomputable ``tasteAdjustments``).

The request/response contract is plain JSON-able dicts, so the *same* adapter runs
against a recorded fixture (deterministic build + tests) or a live LLM (production)
— the only thing that changes is ``model_fn``. Its quality is measured, not
asserted: U8's eval harness re-runs the live scores over Finn's calibration set
and checks the rank correlation still holds (verify-by-nature).

Response contract (what ``model_fn`` must return; one pass):
    {
      "axes": { "<axis>": {"score": 0-10, "contribution": "<one line>"}, ... ×8 },
      "namedLoveHits":   ["<love>", ...],          # profile.lovesNamed found in evidence
      "antiSignalHits":  [["<signal>", <penalty>, <fatal:bool>], ...],
      "staged":          <bool>
    }
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from gaff_engine.ingest import UNTRUSTED_LISTING_NOTE
from gaff_engine.schemas import TasteAxis
from gaff_engine.taste import AXIS_ORDER, AxisRead, TasteRead, _g, _text_blob

# The read request carries the rubric the model scores against — the axis weights
# + scoringNotes (from profile.json / person.taste). Bundled here so a live prompt
# and a recorded fixture share one shape.

_AXIS_KEYS = [a.value for a in AXIS_ORDER]


def _scoring_notes(person: Any) -> Dict[str, str]:
    notes = _g(person, "taste.scoringNotes", "scoringNotes")
    return {str(k): str(v) for k, v in notes.items()} if notes else {}


def build_request(listing: Any, person: Any, *, use_images: bool) -> Dict[str, Any]:
    """The structured taste-read request: the rubric (weighted axes + scoringNotes)
    + the Listing evidence + the Person's named loves / anti-signals. A live prompt
    renders this; a recording is keyed by it. Pure + JSON-able."""
    weights = _g(person, "taste.weights", "weights") or {}
    notes = _scoring_notes(person)
    axes = [{"axis": k, "weight": float(weights.get(k, 0.0)), "rubric": notes.get(k, "")}
            for k in _AXIS_KEYS]
    anti = _g(person, "taste.antiSignals", "antiSignals") or []
    return {
        "schemaVersion": "taste.read.request@1",
        "listingKey": _g(listing, "listingKey", "id"),
        "useImages": bool(use_images),
        "axes": axes,
        "lovesNamed": list(_g(person, "taste.lovesNamed", "lovesNamed") or []),
        "antiSignals": [_g(a, "signal", default=a) for a in anti],
        # Injection guard: evidence text is authored by the listing's agent
        # and must be read as data, not as instructions to the model.
        "evidenceNote": UNTRUSTED_LISTING_NOTE,
        "evidence": {
            "propertyType": str(_enum(_g(listing, "propertyType")) or ""),
            "beds": _g(listing, "beds"), "baths": _g(listing, "baths"),
            "receptions": _g(listing, "receptions"), "sqft": _g(listing, "sqft"),
            "newBuild": bool(_g(listing, "buy.newBuild", "newBuild", default=False)),
            "keyFeatures": list(_g(listing, "keyFeatures") or []),
            "description": _g(listing, "description") or "",
            "text": _text_blob(listing),
        },
    }


def _enum(v: Any) -> Any:
    return getattr(v, "value", v)


def parse_response(response: Dict[str, Any]) -> TasteRead:
    """Validate + parse a model response into a :class:`TasteRead`. Raises if a
    response omits an axis (all eight are required, §5.7 rule 2) or a score is out
    of range — a live model that returns garbage fails loudly, not silently."""
    axes_in = response.get("axes") or {}
    axes: Dict[str, AxisRead] = {}
    missing = []
    for key in _AXIS_KEYS:
        row = axes_in.get(key)
        if row is None:
            missing.append(key)
            continue
        score = float(row["score"])
        if not (0.0 <= score <= 10.0):
            raise ValueError("axis %s score %s out of range [0,10]" % (key, score))
        axes[key] = AxisRead(score=score, contribution=row.get("contribution"))
    if missing:
        raise ValueError("taste model response missing axes %s (all eight required)" % missing)
    anti = response.get("antiSignalHits")
    anti_hits = None if anti is None else [(str(s), float(p), bool(f)) for (s, p, f) in anti]
    return TasteRead(
        axes=axes,
        namedLoveHits=response.get("namedLoveHits"),
        antiSignalHits=anti_hits,
        staged=bool(response.get("staged", False)),
    )


class LiveTasteModel:
    """A live ``TasteModel``: builds the read request, calls ``model_fn``, parses
    the response → :class:`TasteRead`. Drop-in for the recorded model — the engine
    calls it identically (``score(..., taste_model=LiveTasteModel(my_llm))``).

    ``model_fn(request, *, use_images) -> response`` is the LLM boundary. Optional
    ``recorder`` (a dict) captures each ``{request_key: response}`` so a live run
    can be frozen into a deterministic replay for the byte-idempotent build."""

    def __init__(self, model_fn: Callable[..., Dict[str, Any]],
                 recorder: Optional[Dict[str, Any]] = None):
        self._model_fn = model_fn
        self._recorder = recorder

    def __call__(self, listing: Any, person: Any, *, use_images: bool) -> TasteRead:
        request = build_request(listing, person, use_images=use_images)
        response = self._model_fn(request, use_images=use_images)
        if self._recorder is not None:
            key = "%s|%s" % (request.get("listingKey"), "img" if use_images else "text")
            self._recorder[key] = response
        return parse_response(response)


def replay_model(recordings: Dict[str, Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    """A deterministic ``model_fn`` that replays recorded responses keyed by
    ``"<listingKey>|img"`` / ``"<listingKey>|text"`` — the frozen form of a live
    run, for tests + the byte-idempotent build."""
    def _fn(request: Dict[str, Any], *, use_images: bool) -> Dict[str, Any]:
        key = "%s|%s" % (request.get("listingKey"), "img" if use_images else "text")
        if key not in recordings:
            raise KeyError("no recorded taste response for %s" % key)
        return recordings[key]
    return _fn


__all__ = ["LiveTasteModel", "build_request", "parse_response", "replay_model"]
