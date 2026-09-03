"""The real Gaff engine — Milestone M1 (03-engine §5.0). All scorers live.

M0 proved the *pipe* with a stub (:func:`gaff_engine.stub.stub_score` returns the
golden fixture verbatim). M1 replaces the stub body behind the **same signature**
``score(listing, person, search) -> ScoreResult`` with the genuine engine — the
three Mix scorers plus the Forensics shared pre-compute all run for real:

* **Rules** — real, deterministic gate evaluation (U4, :func:`rules_result`): the
  0-10 preference-fit component + the hard-gate exclusion contract.
* **Value Verdict** — real, computed off HM Land Registry Price Paid + EPC comps
  (U3, :func:`value_verdict` on :func:`load_enriched_comps`): the fair estimate,
  the honest headline->adjusted delta, the ``steal|fair|over`` tag, the gauge
  fields and the comp-driven confidence.
* **Taste** — real, the proven v3 pipeline (U6, :func:`taste_result`): per-axis
  reads → weighted base → named-love bonus → anti-signal penalties → learned-rule
  caps, emitting the eight-row ``axisBreakdown`` + a recomputable
  ``tasteAdjustments`` + the text-only ``prior`` (the round-2 image ablation).
  The taste *arithmetic* is fully live and unit-tested; the per-axis *judgement*
  is an LLM read, supplied here by ``taste_model`` — defaulting to
  :func:`canonical_model`, a deterministic **recording** of the reads that
  produced the golden De Beauvoir verdict (so the build is byte-idempotent). A
  live LLM model plugs into the same seam for a new listing; the recorded
  judgement's quality is *measured*, not asserted — U8's eval harness reproduces
  **MAE 1.35 / Spearman 0.79** on Finn's calibration set (§5.1 stage 6 / §7), and
  that calibration is carried on the result (``.tasteCalibration``) so the card
  can cite it. This is verify-by-nature: deterministic math → unit tests, LLM
  judgement → eval harness.
* **Forensics** — the vision read (U7, :func:`forensics_for`): the fourth scorer,
  and the odd one out — it does NOT feed the Mix. It is a shared pre-compute run
  once per ``listingKey`` (Person-independent) that produces the viewing flags
  (``lower_ground_light``, ``north_facing``, ``hmo_history``) and the fatal
  ``cheap_careless_spec`` — a cheap flip the photos reveal routes into taste's
  fatal anti-signal (taste ≤ 2.0), so a listing that clears the declared gates
  but is a white-box flip is quietly killed at the Mix. Runs *after* the gate
  check (no vision spend on an excluded listing, §5.0 step 2→3). Default
  ``forensics_model`` is the canonical recording; a live vision model plugs in.

The composite is the *real* Scorer-Mix (U5, :func:`composite`) over the three
component scores at the Search's ``scorerMix`` (Buy = 55/20/25). The headline
number is genuinely ``taste·55 + rules·20 + value·25`` from three live scorers.

EXCLUSION CONTRACT (03-engine §5.0 step 2 / P1). A failed *hard* Gate
short-circuits the engine: no taste/value spend, a ``score.result@1`` with
``rules.excluded=True``, ``gatesPassed=False``, a *forced* ``composite=0`` (not
recomputed) and null ``taste``/``valueVerdict``. :func:`score` checks this first
and returns an excluded result before loading any comps.

PROVENANCE (don't fake liveness). The taste-read provenance is carried honestly
in two serialisable places so the UI can show it:

* ``components[taste_breakdown].sources[0].label`` names the taste pipeline and
  its calibration — survives serialisation, schema-valid;
* a plain-English marker string on the returned result's convenience attribute
  ``.provenanceNote`` (mirroring how :func:`rules_result` attaches
  ``.reasons``/``.flags``; the validator/serialiser read only declared fields, so
  it does not pollute the contract) — ``build_m1.py`` lifts it into the card.
  :data:`ENGINE_MODE` / :data:`TASTE_READ_NOTE` are the canonical words.

Pure except for the one comps-loader read (same I/O boundary U3 already owns);
deterministic (a fixed :data:`SCORED_AT` and a content-derived id, so re-runs are
byte-idempotent, mirroring ``build_m0.py``).
"""

from __future__ import annotations

import copy
import hashlib
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, List, Optional, Tuple

from gaff_engine.composite import composite as composite_mix
from gaff_engine.eval import CLAIMS
from gaff_engine.forensics import (
    canonical_model as canonical_forensics_model, fatal_anti_signals,
    forensics_flags, forensics_for,
)
from gaff_engine.rules import rules_result
from gaff_engine.schemas import (
    Availability, ComponentName, ComponentSource, ConfidenceReport, Flag,
    FlagCode, FlagKind, FlagSeverity, Reason, Ref, ScoreRequest, ScoreResult,
    ScorerMix, SourcedComponent, TasteResult, ValueTag, ValueVerdict,
)
from gaff_engine.taste import canonical_model, taste_result
from gaff_engine.value import (
    load_enriched_comps, select_anchor, value_score, value_verdict,
)
from gaff_engine.rent import load_rent_pool, rent_cohort, rent_verdict
from gaff_engine.financial import financial_verdict, load_invest_pool, yield_cohort

# ---------------------------------------------------------------------------
# Milestone provenance — the canonical honest words + deterministic stamps.
# ---------------------------------------------------------------------------

ENGINE_MODE = "live"  # all three scorers run their real pipelines (M1 gate met)
# The taste calibration this engine stands on (U8 reproduces it from the recorded
# blind rounds): the image-informed round-2 numbers.
TASTE_CALIBRATION = {
    "mae": CLAIMS["round2_final"]["mae"],        # 1.35
    "spearman": CLAIMS["round2_final"]["spearman"],  # 0.79
    "round": "round2 (text+images), n=%d" % CLAIMS["round2_final"]["n"],
}
RESULT_DISCLAIMER = (
    "A taste-and-value read to inform your own judgement — not a valuation, survey, "
    "mortgage assessment or regulated financial advice.")


def _taste_calibration_phrase(mode: str) -> str:
    """Honest taste-calibration wording (A3): the MAE/Spearman figure was measured on the
    BUY reference set only — it is a property of the MODEL, not a measurement of THIS
    listing, and it is NOT cited for rent/invest, which the model was never calibrated on."""
    if mode == "buy":
        return ("model calibrated on the buy reference set: MAE %.2f / Spearman %.2f (n=%d) "
                "— the model's measured accuracy, not this listing's"
                % (TASTE_CALIBRATION["mae"], TASTE_CALIBRATION["spearman"], CLAIMS["round2_final"]["n"]))
    return "taste model not separately calibrated for %s (buy reference set only)" % mode


def _taste_read_note(mode: str) -> str:
    return ("taste: live v3 pipeline; per-axis read is the canonical recording for this "
            "subject (a live model plugs in for new listings); " + _taste_calibration_phrase(mode))


# Buy-mode default, kept as an importable module symbol (back-compat).
TASTE_READ_NOTE = _taste_read_note("buy")
# Fixed so the M1 build is byte-idempotent (no wall-clock), mirroring build_m0.py.
SCORED_AT = "2026-07-14T12:00:00Z"

_PLUS, _MINUS = "+", "−"  # polarity glyphs (U+2212 matches the golden reasons)

# Buy default Mix if a Search somehow omits scorerMix (03-engine §5.6).
_DEFAULT_MIX = ScorerMix(taste=55, rules=20, value=25)


# ---------------------------------------------------------------------------
# Deterministic numeric helper + duck-typed accessor (codebase style, cf.
# value._g / rules._g — read a dataclass, dict or namespace alike).
# ---------------------------------------------------------------------------

def _round2(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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


def _confidence_band(scalar: float) -> str:
    if scalar >= 0.75:
        return "high"
    if scalar >= 0.5:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Deterministic identity — a content-derived score id + search config hash so
# re-runs are byte-stable (no ULID/clock), mirroring build_m0's determinism.
# ---------------------------------------------------------------------------

def _score_id(listing: Any, person: Any, search: Any) -> str:
    seed = "|".join(str(_g(x, "id", default="")) for x in (listing, person, search))
    return "score_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:26].upper()


def _config_hash(search: Any) -> str:
    mix = _mix(search)
    gates = _g(search, "gates", default=[]) or []
    seed = "%s|%s/%s/%s|%s" % (
        _g(search, "id", default=""), mix.taste, mix.rules, mix.value,
        ",".join("%s%s%s" % (_g(g, "code"), _g(g, "op"), _g(g, "value")) for g in gates),
    )
    return "cfg_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:4]


def _mix(search: Any) -> ScorerMix:
    m = _g(search, "scorerMix")
    return m if isinstance(m, ScorerMix) else _DEFAULT_MIX


def _request(listing: Any, person: Any, search: Any) -> ScoreRequest:
    return ScoreRequest(
        listingRef=Ref(id=_g(listing, "id"), schemaVersion="listing@1"),
        personRef=Ref(id=_g(person, "id"), schemaVersion="person@1"),
        searchRef=Ref(id=_g(search, "id"), schemaVersion="search@1"),
        personVersionSnapshot=_g(person, "profile.version"),
        searchConfigHash=_config_hash(search),
    )


# ---------------------------------------------------------------------------
# Reasons — real (value + rules) plus the reused-golden taste reasons (stub).
# ---------------------------------------------------------------------------

def _value_reasons(vv: ValueVerdict) -> List[Reason]:
    """Turn the Value Verdict's plain-English ``.reasons`` (U3) into schema
    Reasons. The first line (the £/sqft-vs-street headline) is polarity ``+`` when
    the listing is under the street, ``-`` when over; the caveat lines (lease,
    thin comps, confidence) are ``-``. The lease line is pointed at the
    ``lease_adj`` evidence item (P1 intra-object pointer, §5.0), so the card can
    surface it as the honest headline-vs-adjusted note."""
    lines = list(getattr(vv, "reasons", None) or [])
    has_lease_ev = any(getattr(e, "kind", None) == "lease_adj" for e in (vv.evidence or []))
    under_street = (vv.headlineDeltaPct is not None and vv.headlineDeltaPct <= 0)
    out: List[Reason] = []
    for i, text in enumerate(lines):
        refs = ["lease_adj"] if (has_lease_ev and "lease" in text.lower()) else None
        polarity = _PLUS if (i == 0 and under_street) else _MINUS
        out.append(Reason(scorer="value", polarity=polarity, text=text, evidenceRefs=refs))
    return out


def _taste_reasons(taste: TasteResult) -> List[Reason]:
    """The taste-scorer reason lines — the real ones U6 derives from the computed
    axis breakdown + adjustments (attached as ``taste.reasons``). Deep-copied so
    the returned result never aliases the scorer's own list."""
    return [copy.deepcopy(r) for r in (getattr(taste, "reasons", None) or [])
            if getattr(r, "scorer", None) == "taste"]


# ---------------------------------------------------------------------------
# Flags — the rules flags (U4) + the forensics viewing flags (U7) + the small
# listing-field flags (EPC below C). Merged and deduped by (code, source).
# ---------------------------------------------------------------------------

_EPC_C_THRESHOLD = 69  # EPC current score < 69 == a rating below C (buy.epc)


def _listing_flags(listing: Any) -> List[Flag]:
    """Flags read straight off declared Listing fields (not a vision read): the
    EPC-below-C negotiation lever (§5.5b's ``epc_below_c`` info flag)."""
    out: List[Flag] = []
    epc_current = _g(listing, "buy.epc.current", "epc.current")
    epc_potential = _g(listing, "buy.epc.potential", "epc.potential")
    if epc_current is not None and int(epc_current) < _EPC_C_THRESHOLD:
        rating = _g(listing, "buy.epc.rating", "epc.rating")
        pot = (" potential C (%d)." % int(epc_potential)) if epc_potential is not None else "."
        out.append(Flag(
            code=FlagCode.EPC_BELOW_C, severity=FlagSeverity.INFO, kind=FlagKind.LISTING,
            source="buy.epc",
            text="EPC %s (%d);%s Not a blocker, a negotiation lever."
                 % (rating or "below C", int(epc_current), pot)))
    return out


def _merge_flags(*groups: List[Flag]) -> List[Flag]:
    """Concatenate flag groups, deduping by ``(code, source)`` (first wins). Order
    is preserved so the caller controls precedence (rules → forensics → listing)."""
    seen = set()
    out: List[Flag] = []
    for group in groups:
        for f in (group or []):
            key = (getattr(f.code, "value", f.code), f.source)
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Confidence — the real value + rules scalars, the reused taste scalar (stub),
# and a mix-weighted overall (confidence in the composite propagates by the same
# weights that build the composite).
# ---------------------------------------------------------------------------

def _drop_value_weight(mix: ScorerMix) -> Tuple[float, float, float]:
    """Reweight a Mix onto taste+rules only (value dropped), renormalised to sum 100 —
    used when the value verdict is NEEDS_DATA (A2) so an unpriceable home is scored on
    the scorers we can actually compute, not penalised by a zero value component.
    Returns a (taste, rules, value) weight triple, which ``composite`` accepts."""
    mt = float(getattr(mix, "taste", 0.0) or 0.0)
    mr = float(getattr(mix, "rules", 0.0) or 0.0)
    denom = mt + mr
    if denom <= 0:
        return (50.0, 50.0, 0.0)
    return (mt / denom * 100.0, mr / denom * 100.0, 0.0)


def _overall_confidence(taste_c: float, rules_c: float, value_c: float,
                        mix: ScorerMix) -> float:
    weighted = (taste_c * mix.taste + rules_c * mix.rules + value_c * mix.value) / 100.0
    return _round2(weighted)


# ---------------------------------------------------------------------------
# Components — what the engine can feed + honest sources (§5.5b). The value/comps
# rows are live from HM Land Registry + EPC; the taste row carries the stub fact.
# ---------------------------------------------------------------------------

def _comps_freshness(comps: List[Any]) -> Optional[str]:
    dates = [c.sourceDate for c in comps if getattr(c, "sourceDate", None)]
    return max(dates) if dates else None


def _live_components(comps: List[Any], forensics: Any = None, *,
                     value_label: str = "HM Land Registry Price Paid + EPC register"
                     ) -> List[SourcedComponent]:
    fresh = _comps_freshness(comps or [])
    return [
        SourcedComponent(
            component=ComponentName.VALUE_VERDICT, availability=Availability.READY,
            sources=[ComponentSource(label=value_label, freshness=fresh)]),
        SourcedComponent(
            component=ComponentName.COMPS_TABLE, availability=Availability.READY,
            sources=[ComponentSource(label="HM Land Registry Price Paid", freshness=fresh)]),
        SourcedComponent(
            component=ComponentName.LEASE_EXPLAINER, availability=Availability.READY,
            sources=[ComponentSource(label="listing tenure")]),
        SourcedComponent(
            component=ComponentName.RISK_FLAGS, availability=Availability.READY,
            sources=[ComponentSource(label="rules gates + forensics (floorplan/photo) + listing tenure/EPC")]),
        SourcedComponent(
            component=ComponentName.IMAGERY, availability=Availability.READY,
            sources=[ComponentSource(label="forensics vision read (once per listingKey)"
                     + ("" if forensics is None or not getattr(forensics, "aspect", None)
                        else " — aspect %s" % forensics.aspect))]),
        SourcedComponent(
            component=ComponentName.TASTE_BREAKDOWN, availability=Availability.READY,
            sources=[ComponentSource(
                label="profile.json v3 taste pipeline (calibrated MAE %.2f / Spearman %.2f)"
                % (TASTE_CALIBRATION["mae"], TASTE_CALIBRATION["spearman"]))]),
        SourcedComponent(
            component=ComponentName.COMMUTE_ISOCHRONE, availability=Availability.NEEDS_DATA,
            sources=[ComponentSource(label="TravelTime (not yet run)")]),
    ]


def _excluded_components() -> List[SourcedComponent]:
    return [
        SourcedComponent(
            component=ComponentName.RISK_FLAGS, availability=Availability.READY,
            sources=[ComponentSource(label="rules gates")]),
        SourcedComponent(
            component=ComponentName.VALUE_VERDICT, availability=Availability.NEEDS_DATA,
            sources=[ComponentSource(label="skipped: excluded before comp spend (§5.0 step 2)")]),
        SourcedComponent(
            component=ComponentName.TASTE_BREAKDOWN, availability=Availability.NEEDS_DATA,
            sources=[ComponentSource(label="skipped: excluded before taste spend (§5.0 step 2)")]),
    ]


# ---------------------------------------------------------------------------
# The excluded result (P1 exclusion contract, §5.0 step 2).
# ---------------------------------------------------------------------------

def _excluded_result(listing: Any, person: Any, search: Any, rr: Any) -> ScoreResult:
    """A hard-gate-excluded ``score.result@1``: forced ``composite=0``, null
    ``taste``/``valueVerdict``, ``rules`` carrying the failing-gate reasons. No
    taste/value spend was incurred (the unit-economics rule, §5.0)."""
    rules_c = float(getattr(rr, "confidence", 0.85))
    confidence = ConfidenceReport(
        overall=rules_c, taste=0.0, value=0.0, rules=rules_c,
        drivers=["hard-gate exclusion decided on verified listing fields "
                 "(rules confidence %.2f)" % rules_c],
        missing=["excluded before taste/value spend (03-engine §5.0 step 2)"])
    result = ScoreResult(
        id=_score_id(listing, person, search),
        request=_request(listing, person, search),
        composite=0.0,                       # forced, not recomputed (§5.7 rule 1)
        rules=rr,
        reasons=list(getattr(rr, "reasons", None) or []),
        flags=list(getattr(rr, "flags", None) or []),
        confidence=confidence,
        components=_excluded_components(),
        scoredAt=SCORED_AT,
        taste=None,                          # null on exclusion (§5.0 step 2)
        valueVerdict=None,                   # null on exclusion (§5.0 step 2)
    )
    # Convenience attributes the build reads (not schema fields; see module docstring).
    result.engineMode = ENGINE_MODE
    result.provenanceNote = ("excluded by a hard gate (composite forced 0; "
                             "taste/value skipped); " + TASTE_READ_NOTE)
    result.anchorCount = 0
    result.anchorLabel = None
    return result


# ---------------------------------------------------------------------------
# The engine.
# ---------------------------------------------------------------------------

def score(listing: Any, person: Any, search: Any, *,
          taste_model: Any = None, forensics_model: Any = None,
          comps: Any = None) -> ScoreResult:
    """The real M1 engine: ``(listing, person, search) -> ScoreResult`` (03-engine
    §5.0), same signature as :func:`gaff_engine.stub.stub_score`.

    1. Rules first (U4). A hard-gate failure short-circuits to an excluded result
       (forced ``composite=0``, null taste/value) before any comp spend.
    2. Value Verdict (U3) on the real HM Land Registry + EPC comps -> a live
       :class:`ValueVerdict`; :func:`value_score` -> the 0-10 value component.
    3. Taste (U6, :func:`taste_result`) — the real v3 pipeline over ``taste_model``.
       ``taste_model`` defaults to :func:`canonical_model` (the deterministic
       recording that reproduces the golden read); a live LLM model plugs into the
       same seam for a new listing. Taste math is unit-tested; the read's quality
       is measured by U8's eval harness (MAE 1.35 / Spearman 0.79).
    4. Composite (U5) = the real Scorer-Mix over (taste, rules, value) at the
       Search's ``scorerMix``.
    5. A schema-valid ``score.result@1``: three live scorers, real composite, and a
       ConfidenceReport combining the real per-scorer confidences (mix-weighted
       overall). The taste calibration is carried on ``.tasteCalibration``.
    """
    rr = rules_result(listing, search)
    if rr.excluded:
        return _excluded_result(listing, person, search, rr)

    # --- Forensics (U7, §5.5 step 3) — the vision read, once per listingKey,
    #     Person-independent. Feeds the viewing flags + the fatal cheap-flip that
    #     routes into taste's fatal anti-signal (a flip that clears the gates is
    #     killed via taste ≤ 2.0, not silently passed).
    forensics = forensics_for(listing, forensics_model or canonical_forensics_model())
    forensics_fatal = fatal_anti_signals(forensics)

    # --- Value (real, mode-aware) -----------------------------------------
    # BUY reads HM Land Registry sold comps (the truth layer). RENT reads the
    # asking-rent spread on the street (a let is not a purchase of the lease) —
    # the same value_verdict@1 shape, the same 0-10 Mix input, a lighter basis.
    # The Search's mode picks the slot; one engine, two value sources.
    mode = _g(search, "mode", default="buy")
    mode = str(getattr(mode, "value", mode) or "buy")
    if mode == "rent":
        pool = comps if comps is not None else load_rent_pool()
        anchor = rent_cohort(listing, pool)              # the area lets (for the confidence line)
        anchor_label = "comparable lets"
        vv = rent_verdict(listing, pool)
        value_label = "asking-rent spread (rental pool, no Land Registry)"
    elif mode == "invest":
        # INVEST's value slot is the Financial scorer (§5.4): the deal's yield vs the
        # local BTL median, not a purchase-price verdict. High yield → steal.
        pool = comps if comps is not None else load_invest_pool()
        anchor = yield_cohort(listing, pool)             # the local BTL yield cohort
        anchor_label = "comparable BTL deals"
        vv = financial_verdict(listing, pool)
        value_label = "yield vs local median (BTL analysis, financing not modelled)"
    else:
        # Default: the reconciled De Beauvoir cache (offline, keeps the golden +
        # tests deterministic). Live scoring injects on-demand comps via ``comps``.
        if comps is None:
            comps = load_enriched_comps()
        anchor, anchor_label = select_anchor(comps, listing)
        vv = value_verdict(listing, comps)
        value_label = "HM Land Registry Price Paid + EPC register"
    # A subject that can't be priced — no floor area, no asking price, or no comps —
    # yields a NEEDS_DATA verdict rather than a crash (A2). Value is DROPPED from the
    # Mix and its weight reweighted onto taste+rules, so an unpriceable home still
    # scores honestly on what we know instead of being zero-dragged.
    value_missing = getattr(vv, "tag", None) == ValueTag.NEEDS_DATA
    if value_missing:
        value_component = 0.0
    else:
        # invest's financial score is set directly (yield inverts value_score's low=good);
        # the value_verdict / rent verdict already carry a matching .score.
        value_component = vv.score if mode == "invest" else value_score(vv)

    # --- Taste (real v3 pipeline, U6) — fed Forensics' fatal signals (U7) ----
    taste = taste_result(listing, person, taste_model or canonical_model(),
                         extra_anti_signals=forensics_fatal)

    # --- Composite (real Mix) ---------------------------------------------
    mix = _mix(search)
    eff_mix = _drop_value_weight(mix) if value_missing else mix
    comp = composite_mix(taste.score, rr.score, value_component, eff_mix)

    # --- Confidence (real per-scorer scalars, mix-weighted overall) --------
    value_c = float(vv.confidence)
    rules_c = float(getattr(rr, "confidence", 0.85))
    taste_c = float(getattr(taste, "confidence", 0.80))
    overall = _overall_confidence(taste_c, rules_c, value_c, mix)
    short_label = anchor_label.split(" (")[0]
    prior_note = ("" if taste.prior is None
                  else "; text-only prior %.1f (image lift %+.1f)"
                  % (taste.prior, taste.score - taste.prior))
    confidence = ConfidenceReport(
        overall=overall, taste=taste_c, value=value_c, rules=rules_c,
        drivers=[
            "%d %s — %s" % (len(anchor), short_label, value_label),
            "rules gates verified (confidence %.2f)" % rules_c,
            "taste %.1f from the v3 pipeline, %s%s"
            % (taste.score, _taste_calibration_phrase(mode), prior_note),
        ],
        missing=[
            "value confidence %s: %d comparables (want >=5)"
            % (_confidence_band(value_c), len(anchor)),
            "taste per-axis read is the recorded calibration read (live model plugs in for new stock)",
        ])

    # --- Assemble ----------------------------------------------------------
    reasons = _taste_reasons(taste) + _value_reasons(vv) + list(getattr(rr, "reasons", None) or [])
    # rules flags + forensics viewing flags (U7) + listing-field flags (EPC), deduped.
    flags = _merge_flags(list(getattr(rr, "flags", None) or []),
                         forensics_flags(forensics, listing),
                         _listing_flags(listing))

    result = ScoreResult(
        id=_score_id(listing, person, search),
        request=_request(listing, person, search),
        composite=comp,
        rules=rr,
        reasons=reasons,
        flags=flags,
        confidence=confidence,
        components=_live_components(anchor, forensics, value_label=value_label),
        scoredAt=SCORED_AT,
        taste=taste,                         # real (U6 v3 pipeline)
        valueVerdict=vv,                     # real
    )
    # Convenience attributes the build reads (not schema fields; see module docstring).
    result.engineMode = ENGINE_MODE
    result.provenanceNote = _taste_read_note(mode)
    result.disclaimer = RESULT_DISCLAIMER        # A3: honest scope line on every result
    result.tasteCalibration = dict(TASTE_CALIBRATION) if mode == "buy" else None
    result.forensics = forensics             # the U7 vision read (once per listingKey)
    result.anchorCount = len(anchor)
    result.anchorLabel = short_label
    return result


__all__ = ["score", "ENGINE_MODE", "TASTE_READ_NOTE", "TASTE_CALIBRATION", "SCORED_AT"]
