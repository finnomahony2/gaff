"""U6 — the Taste scorer (03-engine §5.1). The proven v3 model, productionised.

This is the formalisation of the taste model that measured **MAE 1.27→1.35,
Spearman 0.77→0.79** across the two blind rounds (data/round{1,2}_scores.json,
reproduced by U8 :mod:`gaff_engine.eval`). It answers "is it *mine*?" — the
counterpart to U3's "is it a good *buy*?".

Architecture — a **deterministic pipeline over a pluggable model read** (the
same shape U3/U4 use: pure Python owns the arithmetic + the recompute contract,
the judgement boundary is injected):

* The **per-axis judgement is an LLM read** — for each of the eight
  :class:`TasteAxis` values the model scores the Listing evidence 0-10 against
  that axis's ``scoringNotes`` rubric and writes a one-line ``contribution``.
  That read is a :class:`TasteRead`, produced by a :class:`TasteModel`. A live
  model prompts an LLM; the :func:`canonical_model` used by the engine + tests
  *replays a recording* of the reads that produced the golden De Beauvoir verdict
  (so the build is deterministic and byte-idempotent — the LLM judgement is
  measured for calibration by the eval harness, not re-rolled on every build).
* Everything after the read is **pure, deterministic Python**, reusing U5's
  :func:`gaff_engine.composite.taste_score` weighting: the weighted base, the
  named-love bonus, the anti-signal penalties, the learned-rule caps/splits, and
  crucially the ``tasteAdjustments[]`` assembly that keeps ``taste.score``
  **recomputable** — ``score = clamp(base + Σ tasteAdjustments.delta, 0, 10)``
  (§5.7 rule 2). Every delta between ``base`` and ``score`` is emitted as a
  signed, sourced row; nothing is hidden.

Five-stage pipeline (§5.1):
  1. per-axis score (LLM read)         → axisBreakdown[]
  2. weighted base = Σ(score·weight)/Σ(weight) over all eight axes
  3. named-love bonus  (+0.1 / hit, cap +0.5)
  4. anti-signal penalties (fatal → force ≤ 2.0 + fatal flag; non-fatal dock)
  5. learned-rule caps & splits (new_build_cap, modernist_icon_cap,
     separate_living_room, bed_count_shape, size_threshold — price never enters)
  + the image ablation: a text-only pass → ``taste.prior``, the image pass →
    ``taste.score`` (round-2 mechanism: text 1.64/0.63 → final 1.35/0.79), and a
    ``staged`` flag when photography outruns the described fabric.

Reproduces the golden exactly through the *real* pipeline (not by copying the
fixture): the canonical De Beauvoir reads give ``taste.score = 8.2`` (base 7.90
+0.30 named-love) and ``taste.prior = 7.4`` (text base 7.125 +0.30).

Like :func:`gaff_engine.rules.rules_result` / :func:`gaff_engine.value.value_verdict`,
this returns a schema-valid :class:`TasteResult` with ``.reasons`` / ``.confidence``
attached as convenience attributes (the validator/serialiser read only declared
fields, so they don't pollute the contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from gaff_engine.composite import taste_score
from gaff_engine.schemas import (
    AxisBreakdown, Reason, TasteAdjustment, TasteAxis, TasteResult,
)

# ---------------------------------------------------------------------------
# CONFIG — the taste block of engine.config@1 (03-engine §5.0 / §5.1). The spec
# FIXES the bonus/cap constants; the deterministic-fallback penalty table below
# mirrors profile.json's named penalties (marble −1.0, carpets −0.75, …) and is
# only consulted when a model read doesn't carry structured anti-signal hits.
# ---------------------------------------------------------------------------

CONFIG: Dict[str, Any] = {
    "namedLoveBonusPerHit": 0.1,
    "namedLoveBonusCap": 0.5,
    "fatalTasteCeiling": 2.0,        # a fatal anti-signal forces taste ≤ 2.0 (§5.1 st.4)
    "newBuildCap": 7.0,              # soft ceiling 6.5-7.0; we cap at the top of the band
    "newBuildCapException": 7.0,     # exceptional view/terrace: no extra cap beyond band top
    "modernistIconCap": 4.5,         # respect ≠ wanting to live there (round-1 Barbican)
    "sizeBonusSqftCeiling": 1700,    # no size bonus above ~1700 sqft (round-1 Dunlace)
    "bedShapeIdeal": (3, 4),
    "bedShapeDockAbove": 5,          # >5 beds = mild wrong-shape dock
    "bedShapeDock": -0.5,
    "separateLivingRoomDock": -1.0,  # open-plan-only + receptions<2
    "priorStabilityBand": 0.5,       # |score − prior| ≤ this ⇒ a "stable read" conf bonus
    "scoreClamp": (0.0, 10.0),
    # Deterministic-fallback non-fatal anti-signal penalties (profile.json).
    "antiSignalPenalties": {
        "marble": -1.0, "bedroom carpets": -0.75, "carpets": -0.75,
        "galley kitchen": -1.0, "long thin galley kitchen": -1.0,
        "open-plan-only": -1.0, "shared-only outdoor space": -3.0,
    },
    # Confidence shape (§5.8): a well-evidenced, image-read, stable listing → 0.80.
    "confBase": 0.70, "confImages": 0.10, "confStable": 0.05,
    "confStagedPenalty": -0.15, "confFatalPenalty": -0.30,
    "confFloor": 0.30, "confCeil": 0.95,
}

# The eight axes in canonical order (weights come from person.taste.weights).
AXIS_ORDER: List[TasteAxis] = [
    TasteAxis.LIGHT_AND_VOLUME, TasteAxis.OUTDOOR_SPACE, TasteAxis.CHARACTER_BONES,
    TasteAxis.WIDTH_PROPORTION_FLOW, TasteAxis.STREET_SCENE,
    TasteAxis.RAW_SIZE_THRESHOLD, TasteAxis.DESIGN_FINISH, TasteAxis.STATION_PROXIMITY,
]

_PLUS, _MINUS = "+", "−"  # polarity glyphs (U+2212, matches the golden reasons)


# ---------------------------------------------------------------------------
# Small deterministic helpers (codebase style, cf. rules._g / value._round).
# ---------------------------------------------------------------------------

def _round1(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _round2(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _clamp(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return float(min(hi, max(lo, x)))


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute-or-key among ``names`` (a dotted name walks in)."""
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


def _axis_key(axis: Any) -> str:
    """Wire value of a TasteAxis / str (so a read may key by enum or string)."""
    return getattr(axis, "value", axis)


def _text_blob(listing: Any) -> str:
    """Lower-cased description + keyFeatures, for the deterministic fallbacks."""
    bits: List[str] = []
    desc = _g(listing, "description")
    if desc:
        bits.append(str(desc))
    kf = _g(listing, "keyFeatures")
    if kf:
        bits.extend(str(x) for x in kf)
    return " ".join(bits).lower()


# ---------------------------------------------------------------------------
# The model boundary — the LLM read is a TasteRead; a TasteModel produces one
# per pass (image / text). Live models prompt an LLM; RecordedModel replays a
# recording (canonical De Beauvoir) so the engine build stays deterministic.
# ---------------------------------------------------------------------------

@dataclass
class AxisRead:
    """One axis's LLM judgement: a 0-10 score + a one-line contribution."""
    score: float = None
    contribution: str = None


@dataclass
class TasteRead:
    """A model's raw judgement for ONE pass over a Listing.

    ``axes`` is required (all eight :class:`TasteAxis`, keyed by wire value or
    enum). The evidence hits are optional — when a model omits them the pipeline
    falls back to deterministic keyword matching over the listing text, so a
    read is never silently un-penalised:

    * ``namedLoveHits`` — which ``person.taste.lovesNamed`` the model found;
    * ``antiSignalHits`` — matched anti-signals as ``(signal, penalty, fatal)``;
    * ``openPlanOnly`` / ``modernistIcon`` / ``newBuildException`` — learned-rule
      observations the read can see (floorplan/imagery) that plain fields cannot;
    * ``staged`` — photography outruns the described fabric (§5.1 st.7).
    """
    axes: Dict[Any, AxisRead] = field(default_factory=dict)
    namedLoveHits: Optional[List[str]] = None
    antiSignalHits: Optional[List[Tuple[str, float, bool]]] = None
    openPlanOnly: bool = False
    modernistIcon: bool = False
    newBuildException: bool = False
    staged: bool = False


class RecordedModel:
    """A deterministic :class:`TasteModel`: replays a stored image / text read.

    ``reads`` maps ``use_images`` (bool) → :class:`TasteRead`. If only the image
    read is supplied it is reused for the text pass (a model with no image
    ablation). This is what :func:`canonical_model` returns for the golden.
    """

    def __init__(self, reads: Dict[bool, TasteRead]):
        if True not in reads and False not in reads:
            raise ValueError("RecordedModel needs at least one of the image/text reads")
        self._reads = reads

    def __call__(self, listing: Any, person: Any, *, use_images: bool) -> TasteRead:
        if use_images and True in self._reads:
            return self._reads[True]
        if not use_images and False in self._reads:
            return self._reads[False]
        # Fall back to whichever read exists (a single-pass model).
        return self._reads.get(use_images, self._reads.get(not use_images))


# ---------------------------------------------------------------------------
# Stage 1-2 — the axis breakdown + weighted base (weights from person.taste).
# ---------------------------------------------------------------------------

def _weights(person: Any) -> Dict[str, float]:
    w = _g(person, "taste.weights", "weights")
    if not w:
        raise ValueError("taste_result needs person.taste.weights (the eight axis weights)")
    return {str(k): float(v) for k, v in w.items()}


def _axis_breakdown(read: TasteRead, weights: Dict[str, float]) -> List[AxisBreakdown]:
    """Assemble all eight AxisBreakdown rows in canonical order (§5.7 rule 2:
    every one of the eight must be present, or the base is non-recomputable)."""
    rows: List[AxisBreakdown] = []
    missing = []
    for axis in AXIS_ORDER:
        key = _axis_key(axis)
        ar = read.axes.get(key) or read.axes.get(axis)
        if ar is None:
            missing.append(key)
            continue
        rows.append(AxisBreakdown(
            axis=axis, score=float(ar.score), weight=weights[key],
            contribution=ar.contribution))
    if missing:
        raise ValueError("taste read is missing axes %s (all eight required, §5.7)" % missing)
    return rows


# ---------------------------------------------------------------------------
# Stage 3 — named-love bonus (model hits, or a deterministic keyword fallback).
# ---------------------------------------------------------------------------

# A tiny synonym map so the deterministic fallback catches loves phrased
# differently in listing prose ("skylight" ⊇ "skylit kitchen" love, etc.).
_LOVE_SYNONYMS: Dict[str, List[str]] = {
    "skylit kitchens": ["skylit", "skylight", "roof light", "rooflight"],
    "double-fronted width": ["double-fronted", "double fronted"],
    "curved bay / curved triple glazing": ["bay window", "bay-fronted", "curved bay"],
    "double-height spaces": ["double-height", "double height", "vaulted"],
    "wisteria/kerb planting": ["wisteria", "kerb", "planting", "mature garden"],
    "exposed brick": ["exposed brick"],
    "colour-drenched period rooms": ["colour-drenched", "colour drenched"],
    "Crittall-style glazing": ["crittall"],
    "big terraces on penthouses": ["roof terrace", "terrace"],
    "conservatories": ["conservatory"],
}


def _named_loves(read: TasteRead, listing: Any, person: Any) -> List[str]:
    """The list of named-love hits: the model's if supplied, else a deterministic
    keyword/synonym match of ``person.taste.lovesNamed`` against the listing text."""
    if read.namedLoveHits is not None:
        return list(read.namedLoveHits)
    loves = _g(person, "taste.lovesNamed", "lovesNamed") or []
    blob = _text_blob(listing)
    hits: List[str] = []
    for love in loves:
        needles = _LOVE_SYNONYMS.get(love, [str(love).lower()])
        if any(n in blob for n in needles):
            hits.append(love)
    return hits


def _named_love_adjustment(hits: List[str]) -> Optional[TasteAdjustment]:
    if not hits:
        return None
    per, cap = CONFIG["namedLoveBonusPerHit"], CONFIG["namedLoveBonusCap"]
    delta = min(len(hits) * per, cap)
    # Show up to three source phrases (the drivers), a "· "-joined trail like the golden.
    src = " · ".join(_short_love(h) for h in hits[:3])
    if len(hits) > 3:
        src += " (+%d more)" % (len(hits) - 3)
    return TasteAdjustment(kind="named_love", delta=_round1(delta), source=src)


def _short_love(love: str) -> str:
    """A short label for a named love (the golden sources 'skylit kitchen', 'bay')."""
    short = {
        "skylit kitchens": "skylit kitchen",
        "curved bay / curved triple glazing": "bay",
        "wisteria/kerb planting": "kerb planting",
        "double-fronted width": "double-fronted",
        "big terraces on penthouses": "terrace",
    }
    return short.get(love, love)


# ---------------------------------------------------------------------------
# Stage 4 — anti-signal penalties (fatal forces ≤ 2.0).
# ---------------------------------------------------------------------------

def _anti_signals(read: TasteRead, listing: Any, person: Any) -> List[Tuple[str, float, bool]]:
    """Matched anti-signals as ``(signal, penalty, fatal)``. Model hits if given,
    else a deterministic keyword match: structured ``person.taste.antiSignals``
    first (each ``{signal, penalty, fatal}``), then the CONFIG fallback table."""
    if read.antiSignalHits is not None:
        return [(s, float(p), bool(f)) for (s, p, f) in read.antiSignalHits]
    blob = _text_blob(listing)
    out: List[Tuple[str, float, bool]] = []
    structured = _g(person, "taste.antiSignals", "antiSignals")
    if structured:
        for a in structured:
            sig = _g(a, "signal")
            if sig and str(sig).lower() in blob:
                pen = float(_g(a, "penalty", default=0.0))
                pen = -abs(pen)  # penalties are docks
                out.append((sig, pen, bool(_g(a, "fatal", default=False))))
        return out
    for sig, pen in CONFIG["antiSignalPenalties"].items():
        if sig in blob:
            out.append((sig, float(pen), False))
    return out


# ---------------------------------------------------------------------------
# Stage 5 — learned-rule caps & splits (price NEVER enters — §5.1 / round-1).
# ---------------------------------------------------------------------------

def _cap_delta(running: float, cap: float) -> float:
    """The signed delta that pulls ``running`` down to ``cap`` (0 if already under)."""
    return _round1(min(0.0, cap - running))


def _learned_adjustments(read: TasteRead, listing: Any,
                         running: float) -> List[TasteAdjustment]:
    """Apply the learned caps/splits *in spec order*, each as a signed, sourced
    delta off the running total. Returns only the deltas that actually fire.
    ``condition_axis_split`` is realised as a fatal anti-signal upstream (st.4),
    so it is not re-applied here (see :func:`taste_result`)."""
    adjustments: List[TasteAdjustment] = []

    # new_build_cap — cap at the top of the 6.5-7.0 band (exception lifts nothing
    # beyond the band top here; the exception's job is to *allow* the cap, §5.1).
    if bool(_g(listing, "buy.newBuild", "newBuild", default=False)):
        cap = CONFIG["newBuildCapException"] if read.newBuildException else CONFIG["newBuildCap"]
        d = _cap_delta(running, cap)
        if d < 0:
            adjustments.append(TasteAdjustment(
                kind="learned_rule", delta=d,
                source="new_build_cap (soft ceiling %.1f)" % cap))
            running += d

    # modernist_icon_cap — architectural respect ≠ wanting to live there.
    if read.modernistIcon:
        d = _cap_delta(running, CONFIG["modernistIconCap"])
        if d < 0:
            adjustments.append(TasteAdjustment(
                kind="learned_rule", delta=d,
                source="modernist_icon_cap (%.1f)" % CONFIG["modernistIconCap"]))
            running += d

    # separate_living_room — open-plan-only + receptions < 2 docks.
    receptions = _g(listing, "receptions", default=None)
    if read.openPlanOnly and receptions is not None and int(receptions) < 2:
        d = _round1(CONFIG["separateLivingRoomDock"])  # emit == apply (§5.7 rule 2)
        adjustments.append(TasteAdjustment(
            kind="learned_rule", delta=d,
            source="separate_living_room (open-plan only, %d reception)" % int(receptions)))
        running += d

    # bed_count_shape — mild dock beyond ~5 beds (wrong-shaped for a sharer pair).
    beds = _g(listing, "beds", default=None)
    if beds is not None and int(beds) > CONFIG["bedShapeDockAbove"]:
        d = _round1(CONFIG["bedShapeDock"])  # emit == apply (§5.7 rule 2)
        adjustments.append(TasteAdjustment(
            kind="learned_rule", delta=d,
            source="bed_count_shape (%d beds > %d)" % (int(beds), CONFIG["bedShapeDockAbove"])))
        running += d

    # size_threshold_enforced — informational: we never ADD a size bonus, so there
    # is nothing to cap. Recorded as a no-op note only when clearly over ceiling.
    return adjustments


# ---------------------------------------------------------------------------
# Confidence (§5.8) + reasons (display sugar derived from the real breakdown).
# ---------------------------------------------------------------------------

def _confidence(used_images: bool, score: float, prior: Optional[float],
                staged: bool, fatal: bool) -> float:
    c = CONFIG["confBase"]
    if used_images:
        c += CONFIG["confImages"]
    if prior is not None and abs(score - prior) <= CONFIG["priorStabilityBand"]:
        c += CONFIG["confStable"]
    if staged:
        c += CONFIG["confStagedPenalty"]
    if fatal:
        c += CONFIG["confFatalPenalty"]
    return _round2(_clamp(c, CONFIG["confFloor"], CONFIG["confCeil"]))


def _reasons(breakdown: List[AxisBreakdown], love_adj: Optional[TasteAdjustment],
             docks: List[TasteAdjustment]) -> List[Reason]:
    """Two-to-three honest taste reasons DERIVED from the computed breakdown (not
    fabricated): the strongest axes (+), the named loves (+), the biggest dock (−)."""
    out: List[Reason] = []
    top = sorted(breakdown, key=lambda r: r.score * r.weight, reverse=True)[:2]
    if top:
        out.append(Reason(
            scorer="taste", polarity=_PLUS,
            text="Strongest on %s." % _join_axes(top)))
    if love_adj:
        out.append(Reason(
            scorer="taste", polarity=_PLUS,
            text="Named loves in the listing (%s) — the +%.1f that lifts it above ordinary-nice."
                 % (love_adj.source, love_adj.delta)))
    worst_dock = min((d for d in docks), key=lambda d: d.delta, default=None)
    if worst_dock is not None and worst_dock.delta < 0:
        out.append(Reason(
            scorer="taste", polarity=_MINUS,
            text="%s (%.1f)." % (worst_dock.source, worst_dock.delta)))
    return out


def _join_axes(rows: List[AxisBreakdown]) -> str:
    labels = {
        TasteAxis.LIGHT_AND_VOLUME: "light & volume", TasteAxis.OUTDOOR_SPACE: "outdoor space",
        TasteAxis.CHARACTER_BONES: "character/bones", TasteAxis.WIDTH_PROPORTION_FLOW: "width & flow",
        TasteAxis.STREET_SCENE: "street scene", TasteAxis.RAW_SIZE_THRESHOLD: "size",
        TasteAxis.DESIGN_FINISH: "finish", TasteAxis.STATION_PROXIMITY: "station",
    }
    return " and ".join("%s (%.1f)" % (labels.get(r.axis, _axis_key(r.axis)), r.score) for r in rows)


# ---------------------------------------------------------------------------
# The scorer.
# ---------------------------------------------------------------------------

def taste_result(listing: Any, person: Any, model: Any, *,
                 use_images: bool = True, run_prior: bool = True,
                 extra_anti_signals: Optional[List[Tuple[str, float, bool]]] = None
                 ) -> TasteResult:
    """Score a Listing's *taste* fit for a Person via ``model`` — the real v3
    pipeline (03-engine §5.1), returning a schema-valid :class:`TasteResult`
    (``score``, ``prior``, ``staged``, ``axisBreakdown[]``, ``tasteAdjustments[]``)
    with ``.reasons`` / ``.confidence`` attached as convenience attributes.

    ``model`` is a :class:`TasteModel` — a callable ``(listing, person, *,
    use_images) -> TasteRead`` (a live LLM read, or a :class:`RecordedModel`).
    The image pass yields ``taste.score``; a text-only pass (``run_prior`` and
    ``use_images``) yields ``taste.prior`` — the round-2 ablation.

    ``extra_anti_signals`` are anti-signals sourced *outside* the taste read —
    specifically Forensics' fatal ``cheap_careless_spec`` (U7, §5.5): a cheap flip
    the vision read catches routes here as a fatal anti-signal, forcing taste
    ≤ 2.0 (stage 4). They are merged with the model read's own anti-signals.

    The recompute contract (§5.7 rule 2) holds by construction: ``score ==
    clamp(round(base + Σ tasteAdjustments.delta, 0, 10))``.
    """
    weights = _weights(person)

    # --- Stage 1-2: image pass → axis breakdown + weighted base --------------
    read = model(listing, person, use_images=use_images)
    breakdown = _axis_breakdown(read, weights)
    base = taste_score(breakdown, 0.0)  # weighted base, no adjustments yet (== 7.9 golden)

    # --- Stage 3: named-love bonus ------------------------------------------
    love_adj = _named_love_adjustment(_named_loves(read, listing, person))
    adjustments: List[TasteAdjustment] = []
    running = base
    if love_adj is not None:
        adjustments.append(love_adj)
        running += love_adj.delta

    # --- Stage 4: anti-signal penalties (fatal → force ≤ 2.0) ---------------
    # The read's own anti-signals plus any fed in from Forensics (§5.5 fatal flip).
    all_anti = list(_anti_signals(read, listing, person)) + list(extra_anti_signals or [])
    fatal = False
    for sig, pen, is_fatal in all_anti:
        # Round ONCE and apply the SAME number we emit: an off-grid penalty
        # (e.g. carpets −0.75) must advance the running total by exactly the
        # delta the row shows, or §5.7 rule 2 (score recomputable from the
        # emitted rows) silently breaks by a tenth.
        d = _round1(pen)
        adjustments.append(TasteAdjustment(
            kind="anti_signal", delta=d, source=sig))
        running += d
        if is_fatal:
            fatal = True
    if fatal:
        d = _cap_delta(running, CONFIG["fatalTasteCeiling"])
        if d < 0:
            adjustments.append(TasteAdjustment(
                kind="fatal_cap", delta=d,
                source="fatal anti-signal forces taste ≤ %.1f" % CONFIG["fatalTasteCeiling"]))
            running += d

    # --- Stage 5: learned-rule caps & splits --------------------------------
    learned = _learned_adjustments(read, listing, running)
    adjustments.extend(learned)
    running += sum(a.delta for a in learned)

    # --- Final score (recompute contract: base + Σ delta) -------------------
    score = _round1(_clamp(running, *CONFIG["scoreClamp"]))

    # --- Prior (text-only pass) — same evidence-derived deltas on the text base
    prior: Optional[float] = None
    if run_prior and use_images:
        text_read = model(listing, person, use_images=False)
        text_breakdown = _axis_breakdown(text_read, weights)
        base_prior = taste_score(text_breakdown, 0.0)
        delta_sum = round(sum(a.delta for a in adjustments), 4)
        prior = _round1(_clamp(base_prior + delta_sum, *CONFIG["scoreClamp"]))

    docks = [a for a in adjustments if a.delta < 0]
    result = TasteResult(
        score=score,
        prior=prior,
        staged=bool(read.staged),
        axisBreakdown=breakdown,
        tasteAdjustments=adjustments,
    )
    # Convenience attributes the pipeline reads (not schema fields; cf. rules_result).
    result.confidence = _confidence(use_images, score, prior, bool(read.staged), fatal)
    result.reasons = _reasons(breakdown, love_adj, docks)
    result.base = _round1(base)
    result.fatal = fatal
    return result


# ---------------------------------------------------------------------------
# The canonical De Beauvoir recording — the reads that reproduce the golden
# through the REAL pipeline (score 8.2, prior 7.4). This is what the engine and
# the U6 tests inject: a deterministic replay of the model judgement that
# produced 01-domain §5.5b / 03-engine §5.1's worked example.
# ---------------------------------------------------------------------------

def canonical_deb_reads() -> Tuple[TasteRead, TasteRead]:
    """The (image, text) reads for the golden De Beauvoir maisonette.

    Image pass — the eight axes verbatim from §5.1's table (base 426.5/54.0 =
    7.90) + the three named loves (skylit kitchen, kerb planting, bay → +0.30).
    Text pass — garden/skylight/bay unconfirmed from prose, character read lower
    (weighted base 384.75/54.0 = 7.125 → prior 7.125 + 0.30 = 7.425 → 7.4)."""
    image = TasteRead(
        axes={
            "light_and_volume": AxisRead(8.0, "bay + skylit kitchen; lower-ground darkness risk to check"),
            "outdoor_space": AxisRead(8.5, "private SW garden with inside-outside potential"),
            "character_bones": AxisRead(9.0, "Victorian bones, restored floorboards, bay — distinctive not ordinary-nice"),
            "width_proportion_flow": AxisRead(7.0, "single-fronted terrace; flows over two floors, not double-fronted"),
            "street_scene": AxisRead(8.0, "handsome De Beauvoir terrace"),
            "raw_size_threshold": AxisRead(7.0, "1050 sqft — comfortably over the 900 gate, no size bonus territory"),
            "design_finish": AxisRead(7.0, "restored boards, skylit kitchen, no marble"),
            "station_proximity": AxisRead(7.0, "Dalston Junction 0.5 mi"),
        },
        namedLoveHits=["skylit kitchens", "wisteria/kerb planting", "curved bay / curved triple glazing"],
        antiSignalHits=[],       # none matched on De Beauvoir
        staged=False,
    )
    text = TasteRead(
        axes={
            "light_and_volume": AxisRead(7.0, "bay noted in prose; skylight/volume unconfirmed on paper"),
            "outdoor_space": AxisRead(7.5, "garden stated, quality unseen without images"),
            "character_bones": AxisRead(7.5, "period features stated; intensity unconfirmed from prose"),
            "width_proportion_flow": AxisRead(7.0, "two-floor flow described"),
            "street_scene": AxisRead(7.0, "De Beauvoir terrace known, no map read"),
            "raw_size_threshold": AxisRead(7.0, "1050 sqft stated"),
            "design_finish": AxisRead(6.5, "finish unseen on paper"),
            "station_proximity": AxisRead(7.0, "Dalston Junction 0.5 mi"),
        },
        namedLoveHits=["skylit kitchens", "wisteria/kerb planting", "curved bay / curved triple glazing"],
        antiSignalHits=[],
        staged=False,
    )
    return image, text


def canonical_model() -> RecordedModel:
    """The :class:`RecordedModel` the engine + tests use to reproduce the golden
    taste verdict deterministically (score 8.2, prior 7.4)."""
    image, text = canonical_deb_reads()
    return RecordedModel({True: image, False: text})


__all__ = [
    "CONFIG", "AXIS_ORDER", "AxisRead", "TasteRead", "RecordedModel",
    "taste_result", "canonical_deb_reads", "canonical_model",
]
