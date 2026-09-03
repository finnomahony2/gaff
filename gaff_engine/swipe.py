"""U-swipe — the P4 taste-learning engine (04-elicitation.md §5).

The write-path *generator and applier* that sits behind P1's ``feedback@1``: it
owns everything between a reaction and a receipted profile mutation — which
question to ask next (``select_next_probe``, §5.3), what each gesture means
(``swipe_feedback``, §5.4), how "hate the marble" becomes a re-weight you can
see (``apply_feedback``, §5.5), what the engine does not yet know
(``taste.uncertainty@1`` + the weight-aware clarity meter, §5.2), the warm cold
start (``taste.twin@1``, §5.9), and the reward loop (§5.10).

Two pure functions read one config (§5.0): the **selector** and the **mutation
applier**. Everything is deterministic and stdlib-only, and reproduces the spec's
worked numbers exactly:

* the Petherton right-swipe → ``width σ 3.00→1.30, mean 6.0→8.1`` (§5.5, the
  single-observation trust cap + the coverage-relaxed σ floor);
* the §5.3 selector → the maisonette swipe EIG **18.2** ≫ the one-word tap
  **2.34**, and a voice version **23.1**;
* the §5.2 clarity fixture → **0.68** (the weight-aware residual-uncertainty
  scalar the meter renders and the selector minimises — same quantity);
* the two-mentions-make-it-stick anti-signal rule (marble → −1.0), and the
  twin decay (< 2 % of the belief after ~4 low-noise observations).

The P3 boundary: a swipe's per-axis decomposition is a P3 taste read
(``swipe_feedback`` calls the injected model); ``apply_feedback`` consumes the
resulting observations, so the *maths* is unit-testable in isolation with
hand-built observations (the §5.5 numeric fixture) independent of any model.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from gaff_engine.schemas import (
    AntiSignal, AntiSignalBelief, AxisBelief, Person, Probe, ProbeKind, Ref,
    RewardState, TasteTwin, TastePrior, TasteUncertainty, TwinPrivacy,
    UncertaintyOverall, UncertaintyProvenance,
)

# ---------------------------------------------------------------------------
# elicitation.config@1 — the tunable bundle (§5.0). The artefact the memory rule
# "backtest offline before live" acts on: change a probeNoise or a threshold,
# replay the §7.2 synthetic harness, ship only if learning-speed + calibration
# hold. `probeNoise` is the observation noise τ per Probe kind, in score-points.
# ---------------------------------------------------------------------------

CONFIG: Dict[str, Any] = {
    "schemaVersion": "elicitation.config@1",
    "version": "1.0.0",
    "uncertainty": {"sigma0": 3.0, "sigmaMin": 0.7, "antiSignalConfirmAt": 2, "coverageFloorK": 0.35},
    "probeNoise": {
        "voice_rate": 0.8, "calibration_home": 1.0, "this_or_that": 1.0,
        "forced_rank": 1.0, "swipe_with_why": 1.2, "swipe_bare": 1.8, "one_word": 2.2,
    },
    "selector": {"antiFatiguePenaltyPerRepeat": 0.35, "valueBeforeAsk": True, "diversityWindow": 3,
                 "antiSignalImportance": 5.0},
    "clarity": {"weightByAxisWeight": True},
    "twin": {"minCohort": 50, "dpNoiseSigma": 0.4, "priorSigmaFloor": 2.6, "decayHalfLifeObs": 4},
    "rewards": {"streakGraceHours": 40, "discoveryScoreFloor": 8.0, "discoveryNoveltyMax": 0.35,
                "clarityMilestones": [0.25, 0.5, 0.75, 0.9]},
    "game": {"nHomes": 6, "sealAlgo": "sha256", "revealOnComplete": True},
    "singleObsCap": 0.6,   # §5.5 single-observation trust cap (OQ 8.2)
    "neutralMean": 5.0,    # the flat cold-start prior mean (no twin)
}

# The eight taste axes in canonical weight order (the same axes U6 scores and
# elicit.py ranks). Person weights (importance) come from person.taste.weights.
AXES: List[str] = ["light_and_volume", "outdoor_space", "character_bones",
                   "width_proportion_flow", "street_scene", "raw_size_threshold",
                   "design_finish", "station_proximity"]

_ARCHETYPE_TIERS = ("S", "A", "B", "C")
_NUM_WORD = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}


def _round1(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _round2(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, x)))


def _weights(person: Any) -> Dict[str, float]:
    w = getattr(getattr(person, "taste", None), "weights", None) or {}
    return {str(k): float(v) for k, v in w.items()}


# ---------------------------------------------------------------------------
# The working reaction shapes (this spec's own; the persisted feedback@1 is P1's
# closed { axis, signalDelta, newAntiSignal?, ruleProposed? } — emitted by
# `apply_feedback` as `interpretation`, never carrying the richer vector, A11).
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """One per-axis observation extracted from a reaction: pull `axis` toward
    `value` (0-10) with noise `tau` (probeNoise per Probe kind/richness)."""
    axis: str
    value: float
    tau: float


@dataclass
class Feedback:
    """A reaction, interpreted into observations (§5.5 step 1). `kind` mirrors P1
    feedback@1 (swipe|rating|correction). The per-axis vector, named loves,
    archetype and area are `apply_feedback`'s working; only `primaryAxis` +
    its delta land in the persisted P1 `interpretation`."""
    kind: str = "swipe"
    observations: List[Observation] = field(default_factory=list)
    direction: Optional[str] = None                # right|left|up (swipe)
    antiSignalMentions: List[Tuple[str, float, bool]] = field(default_factory=list)  # (signal, penalty, fatal)
    namedLoves: List[str] = field(default_factory=list)
    archetypeTier: Optional[str] = None            # S|A|B|C — coverage credit
    area: Optional[str] = None
    primaryAxis: Optional[str] = None              # the axis the P1 interpretation records
    ruleProposed: Optional[str] = None
    listingRef: Optional[Ref] = None
    id: Optional[str] = None


@dataclass
class Receipt:
    """feedback@1.receipt (§5.7) — the visible re-weight. Always populated."""
    before: str
    after: str
    summary: str
    scope: str = "this search"      # "this search" | "every search"
    clarityDelta: float = 0.0


# ---------------------------------------------------------------------------
# §5.2 — the clarity scalar (weight-aware residual uncertainty). The meter value
# AND the selector's objective, so the bar the user watches is what the engine
# minimises. Reproduces the §5.2 fixture exactly: 0.68.
# ---------------------------------------------------------------------------

def clarity(uncertainty: TasteUncertainty, weights: Dict[str, float],
            config: Dict[str, Any] = CONFIG) -> float:
    s0 = config["uncertainty"]["sigma0"]
    smin = config["uncertainty"]["sigmaMin"]
    num = 0.0
    den = 0.0
    for axis in AXES:
        w = float(weights.get(axis, 0.0))
        sig = uncertainty.axes[axis].sigma
        num += w * (sig - smin)
        den += w * (s0 - smin)
    if den <= 0:
        return 0.0
    return _round2(_clamp(1.0 - num / den, 0.0, 1.0))


def _drag(uncertainty: TasteUncertainty, weights: Dict[str, float],
          config: Dict[str, Any]) -> List[Tuple[str, float]]:
    """Each axis's contribution to the residual uncertainty, w_a·(σ_a − σMin),
    descending — the selector's `weakestAxes` shortlist (what to ask next)."""
    smin = config["uncertainty"]["sigmaMin"]
    rows = [(axis, float(weights.get(axis, 0.0)) * (uncertainty.axes[axis].sigma - smin))
            for axis in AXES]
    return sorted(rows, key=lambda r: -r[1])


def _sigma_mean(uncertainty: TasteUncertainty) -> float:
    return _round2(sum(uncertainty.axes[a].sigma for a in AXES) / len(AXES))


def _recompute_overall(uncertainty: TasteUncertainty, weights: Dict[str, float],
                       config: Dict[str, Any]) -> None:
    uncertainty.overall = UncertaintyOverall(
        clarity0to1=clarity(uncertainty, weights, config),
        sigmaMean=_sigma_mean(uncertainty),
        weakestAxes=[a for a, _ in _drag(uncertainty, weights, config)[:3]])


# ---------------------------------------------------------------------------
# Seed a fresh taste.uncertainty@1 — flat (clarity 0), or warm from a Taste-twin
# (§5.9): a cohort prior with σ ≥ priorSigmaFloor so it stays honest.
# ---------------------------------------------------------------------------

def seed_uncertainty(person: Any, twin: Optional[TasteTwin] = None,
                     config: Dict[str, Any] = CONFIG) -> TasteUncertainty:
    s0 = config["uncertainty"]["sigma0"]
    neutral = config["neutralMean"]
    axes: Dict[str, AxisBelief] = {}
    for axis in AXES:
        if twin is not None and axis in (twin.priors or {}):
            p = twin.priors[axis]
            axes[axis] = AxisBelief(mean=p.mean, sigma=max(p.sigma, config["twin"]["priorSigmaFloor"]), nObs=0)
        else:
            axes[axis] = AxisBelief(mean=neutral, sigma=s0, nObs=0)
    anti: Dict[str, AntiSignalBelief] = {}
    if twin is not None and (twin.antiSignalPriors or None):
        for sig, meta in twin.antiSignalPriors.items():
            anti[sig] = AntiSignalBelief(leaning=meta.get("leaning", "dislike"),
                                         strength=float(meta.get("strength", 0.0)),
                                         mentions=0, confirmed=False,
                                         sigma=float(meta.get("sigma", 2.0)))
    prov = UncertaintyProvenance(
        seededFromTwin=twin is not None,
        twinRef=Ref(id=twin.id, schemaVersion="taste.twin@1") if twin is not None else None,
        lastProbeAt=None)
    version = int(getattr(getattr(person, "profile", None), "version", 1) or 1)
    unc = TasteUncertainty(
        personRef=Ref(id=person.id, schemaVersion="person@1"),
        profileVersion=version, axes=axes, antiSignals=anti,
        archetypeCoverage={t: 0 for t in _ARCHETYPE_TIERS}, provenance=prov,
        overall=None, areaAffinity={})
    _recompute_overall(unc, _weights(person), config)
    return unc


# ---------------------------------------------------------------------------
# §5.5 — apply_feedback: the mutation-application engine (the data engine core).
# Pure; bumps profile.version; Bayesian per-axis update with the single-obs trust
# cap + coverage-relaxed σ floor; two-mention anti-signal rule; a receipt always.
# ---------------------------------------------------------------------------

def _bayes_axis(belief: AxisBelief, value: float, tau: float,
                config: Dict[str, Any]) -> Tuple[float, float]:
    """The §5.5 step-2 update → (mean', σ'). Reproduces the Petherton numbers:
    prior σ3.0/mean6.0, obs v9.5/τ0.8 → mean 8.1 (0.6 single-obs cap), σ 1.30
    (coverage floor at nObs=1), not the bare Bayesian collapse (0.77)."""
    s0 = config["uncertainty"]["sigma0"]
    smin = config["uncertainty"]["sigmaMin"]
    k = config["uncertainty"]["coverageFloorK"]
    cap = config["singleObsCap"]
    precision = 1.0 / (belief.sigma ** 2)
    obs_precision = 1.0 / (tau ** 2)
    n = belief.nObs + 1
    mean_bayes = (belief.mean * precision + value * obs_precision) / (precision + obs_precision)
    move = mean_bayes - belief.mean
    lim = cap * (value - belief.mean)                       # signed single-obs cap
    move = _clamp(move, -abs(lim), abs(lim))
    mean2 = belief.mean + move
    sigma_bayes = math.sqrt(1.0 / (precision + obs_precision))
    sigma_floor = smin + (s0 - smin) * k / (n + k)          # coverage-relaxed floor → σMin as n→∞
    sigma2 = max(sigma_bayes, sigma_floor)
    return mean2, sigma2


def apply_feedback(person: Any, feedback: Feedback, uncertainty: TasteUncertainty,
                   config: Dict[str, Any] = CONFIG
                   ) -> Tuple[Any, TasteUncertainty, Receipt, Dict[str, Any]]:
    """Apply one reaction → (person', uncertainty', receipt, interpretation).

    Pure: deep-copies both inputs, mutates the copies, bumps
    ``person.profile.version`` by 1 and returns the P1-closed
    ``feedback@1.interpretation`` ({axis, signalDelta, newAntiSignal?,
    ruleProposed?}) alongside. The richer per-axis vector / named loves /
    archetype / area land in ``uncertainty`` + ``person.taste`` — never the
    persisted interpretation (A11)."""
    person = copy.deepcopy(person)
    uncertainty = copy.deepcopy(uncertainty)
    weights = _weights(person)
    prev_clarity = uncertainty.overall.clarity0to1 if uncertainty.overall else 0.0

    # Version bump (P1 §5.6 G): one applied event = +1.
    person.profile.version = int(person.profile.version or 1) + 1
    uncertainty.profileVersion = person.profile.version

    # --- Step 2: Bayesian per-axis update over the observation vector ---------
    primary = feedback.primaryAxis
    primary_before = primary_after = None
    signal_delta = 0.0
    for obs in feedback.observations:
        if obs.axis not in uncertainty.axes:
            continue
        b = uncertainty.axes[obs.axis]
        if primary is None:
            primary = obs.axis
        if obs.axis == primary:
            primary_before = (b.sigma, b.mean)
        mean2, sigma2 = _bayes_axis(b, obs.value, obs.tau, config)
        b.mean, b.sigma, b.nObs = _round2(mean2), _round2(sigma2), b.nObs + 1
        if obs.axis == primary:
            primary_after = (b.sigma, b.mean)
            signal_delta = _round1(b.mean - (primary_before[1] if primary_before else b.mean))

    # --- Step 3: anti-signal rule (two mentions make it stick, §5.5) ---------
    new_anti = None
    confirm_at = config["uncertainty"]["antiSignalConfirmAt"]
    for sig, penalty, fatal in feedback.antiSignalMentions:
        belief = uncertainty.antiSignals.get(sig) or AntiSignalBelief(
            leaning="dislike", strength=float(penalty), mentions=0, confirmed=False, sigma=1.8)
        belief.mentions += 1
        belief.strength = float(penalty)
        belief.leaning = "dislike"
        belief.sigma = _round2(max(config["uncertainty"]["sigmaMin"], belief.sigma * 0.7))
        uncertainty.antiSignals[sig] = belief
        if belief.mentions >= confirm_at and not belief.confirmed:
            belief.confirmed = True
            _write_anti_signal(person, sig, float(penalty), bool(fatal))
            new_anti = sig

    # --- Step 6: coverage, areas, named loves --------------------------------
    if feedback.archetypeTier in _ARCHETYPE_TIERS:
        uncertainty.archetypeCoverage[feedback.archetypeTier] += 1
    if feedback.area:
        aff = uncertainty.areaAffinity or {}
        row = aff.get(feedback.area) or {"leaning": "like", "nObs": 0}
        row["leaning"] = "like" if feedback.direction in ("right", "up", None) else "dislike"
        row["nObs"] = int(row.get("nObs", 0)) + 1
        aff[feedback.area] = row
        uncertainty.areaAffinity = aff
    for love in feedback.namedLoves:
        loves = person.taste.lovesNamed or []
        if love not in loves:
            loves = list(loves) + [love]
        person.taste.lovesNamed = loves

    _recompute_overall(uncertainty, weights, config)

    # --- Step 5: the receipt (the visible change) ----------------------------
    receipt = _make_receipt(primary, primary_before, primary_after, new_anti,
                            confirm_at, uncertainty, prev_clarity, feedback)

    interpretation = {"axis": primary, "signalDelta": signal_delta,
                      "newAntiSignal": new_anti, "ruleProposed": feedback.ruleProposed}
    if feedback.id is not None:
        # stamp appliedToProfileVersion (P1 §5.6 G) alongside — the applier's output
        interpretation["appliedToProfileVersion"] = person.profile.version
    return person, uncertainty, receipt, interpretation


def _write_anti_signal(person: Any, signal: str, penalty: float, fatal: bool) -> None:
    """Write a confirmed anti-signal into person.taste.antiSignals (dedup by name)."""
    existing = person.taste.antiSignals or []
    if any(getattr(a, "signal", None) == signal for a in existing):
        return
    person.taste.antiSignals = list(existing) + [
        AntiSignal(signal=signal, penalty=-abs(penalty), fatal=bool(fatal))]


def _make_receipt(primary, before, after, new_anti, confirm_at, uncertainty,
                  prev_clarity, feedback: Feedback) -> Receipt:
    delta = _round2((uncertainty.overall.clarity0to1 - prev_clarity) if uncertainty.overall else 0.0)
    if new_anti is not None:
        # The exact P1 §5.6 receipt string for a two-mention confirm (A5).
        return Receipt(
            before="%s: unweighted" % new_anti,
            after="%s: named anti-signal, %.1f" % (
                new_anti, next((p for s, p, _ in feedback.antiSignalMentions if s == new_anti), -1.0)),
            summary="%s is now a standing dislike across every Search. %s mentions — I've made it stick." % (
                new_anti.split()[0].capitalize(), _NUM_WORD.get(confirm_at, str(confirm_at))),
            scope="every search", clarityDelta=delta)
    if primary and before and after:
        return Receipt(
            before="%s: σ %.2f, mean %.1f" % (primary, before[0], before[1]),
            after="%s: σ %.2f, mean %.1f" % (primary, after[0], after[1]),
            summary=_axis_summary(primary, before, after, feedback),
            scope="this search", clarityDelta=delta)
    return Receipt(before="—", after="—", summary="Logged.", clarityDelta=delta)


def _axis_summary(axis, before, after, feedback: Feedback) -> str:
    label = axis.replace("_", " ")
    if feedback.direction == "left":
        return "%s — noted, even against the nice parts. Preference now confirmed both ways." % label.capitalize()
    if feedback.namedLoves:
        return "Logged your %s: %s. I'll weight %s harder from here." % (
            (feedback.archetypeTier or "A") + "-tier", ", ".join(feedback.namedLoves[:3]), label)
    return "Read your %s: σ tightened %.2f→%.2f. I know that dimension of your taste better now." % (
        label, before[0], after[0])


# ---------------------------------------------------------------------------
# §5.3 — select_next_probe: EIG argmax under value-before-ask + anti-fatigue.
# Reproduces 18.2 (maisonette swipe) ≫ 2.34 (one-word) and 23.1 (voice).
# ---------------------------------------------------------------------------

def _tau(kind: Any, config: Dict[str, Any]) -> float:
    k = getattr(kind, "value", kind)
    pn = config["probeNoise"]
    if k == "swipe_card":
        return pn["swipe_bare"]           # the guaranteed selection-time floor (§5.0)
    return pn.get(k, pn["swipe_bare"])


def expected_info_gain(probe: Probe, uncertainty: TasteUncertainty,
                       weights: Dict[str, float], recent_axes: Optional[List[List[str]]] = None,
                       config: Dict[str, Any] = CONFIG) -> float:
    """EIG = Σ_{a∈informs} (w_a/10)·(σ_a² − σ_a'²) − antiFatigue, σ_a'² from the
    Bayesian precision update at the kind's τ. Deterministic at selection."""
    tau = _tau(probe.kind, config)
    imp_anti = config["selector"]["antiSignalImportance"]
    gain = 0.0
    informed_axes: List[str] = []
    for inf in (probe.informs or []):
        target = inf.get("target")
        key = inf.get("key")
        if target == "axis" and key in uncertainty.axes:
            sig = uncertainty.axes[key].sigma
            w = float(weights.get(key, 0.0))
            informed_axes.append(key)
        elif target == "antiSignal":
            belief = uncertainty.antiSignals.get(key)
            sig = belief.sigma if belief else 2.0
            w = imp_anti
        else:
            continue
        sig2_post = 1.0 / (1.0 / (sig ** 2) + 1.0 / (tau ** 2))
        gain += (w / 10.0) * (sig ** 2 - sig2_post)
    penalty = _anti_fatigue(informed_axes, recent_axes, config)
    return _round2(gain - penalty)


def _anti_fatigue(informed_axes: List[str], recent_axes: Optional[List[List[str]]],
                  config: Dict[str, Any]) -> float:
    if not recent_axes:
        return 0.0
    window = config["selector"]["diversityWindow"]
    per = config["selector"]["antiFatiguePenaltyPerRepeat"]
    recent = recent_axes[-window:]
    hits = sum(1 for served in recent if any(a in served for a in informed_axes))
    return per * hits


def select_next_probe(uncertainty: TasteUncertainty, pool: List[Probe],
                      person: Any, recent_axes: Optional[List[List[str]]] = None,
                      config: Dict[str, Any] = CONFIG) -> Optional[Probe]:
    """argmax EIG over the value-feasible pool (a bare ask with no valuePayload is
    dropped, §5.1). Ties break to the richer (lower-τ) kind, then a weakest axis."""
    weights = _weights(person)
    weakest = set(uncertainty.overall.weakestAxes if uncertainty.overall else [])
    feasible = []
    for probe in pool:
        if config["selector"]["valueBeforeAsk"] and not probe.valuePayload:
            continue
        eig = expected_info_gain(probe, uncertainty, weights, recent_axes, config)
        probe.expectedInfoGain = eig
        informed = {i.get("key") for i in (probe.informs or [])}
        tie = (eig, -_tau(probe.kind, config), 1 if informed & weakest else 0)
        feasible.append((tie, probe))
    if not feasible:
        return None
    feasible.sort(key=lambda t: t[0], reverse=True)
    return feasible[0][1]


# ---------------------------------------------------------------------------
# §5.4 — the gesture→feedback builder. Decompose a swiped Listing via the P3
# taste model into observations, signed by gesture; tap/voice lower τ.
# ---------------------------------------------------------------------------

def swipe_feedback(listing: Any, person: Any, taste_model: Any, gesture: str, *,
                   why: Optional[str] = None, voiced: bool = False,
                   offending_axis: Optional[str] = None,
                   archetype_tier: Optional[str] = None, informs_k: int = 3,
                   config: Dict[str, Any] = CONFIG) -> Feedback:
    """Build a Feedback from a swipe. Right → positive observations at the P3 axis
    reads; left → negative (the offending axis pulled low, the 'skinny=kill');
    up → positive but taste-only (Dream parking is the deck's job). A tapped/voiced
    why lowers τ (swipe_bare 1.8 → swipe_with_why 1.2 → voice_rate 0.8).

    A swipe informs only its **most salient** axes (weight × distance from neutral),
    plus a named offending axis on a left — not all eight. The axes a home does not
    speak to (a flat station read on a period terrace) stay uncertain until a probe
    actually resolves them (§5.2/§5.3); this is why the meter fills over a handful
    of swipes rather than snapping to certain."""
    read = taste_model(listing, person, use_images=True)
    pn = config["probeNoise"]
    tau = pn["voice_rate"] if voiced else (pn["swipe_with_why"] if why else pn["swipe_bare"])
    weights = _weights(person)
    neutral = config["neutralMean"]

    present = [a for a in AXES if read.axes.get(a) is not None]
    salient = sorted(present, key=lambda a: -(weights.get(a, 0.0) * abs(float(read.axes[a].score) - neutral)))
    informed = list(salient[:max(1, informs_k)])
    if gesture == "left" and offending_axis and offending_axis in present and offending_axis not in informed:
        informed.append(offending_axis)

    anti_present = bool(getattr(read, "antiSignalHits", None))
    obs: List[Observation] = []
    for axis in informed:
        score = float(read.axes[axis].score)
        if gesture == "left":
            if offending_axis is not None:
                value = min(score, 2.5) if axis == offending_axis else score   # the named kill tanks; rest keep the read
            elif anti_present:
                value = score                                                  # an anti-signal carries the dislike, not the axes
            else:
                value = min(score, 5.0)                                        # a bare dislike: a mild uniform dock (never a no-op)
        else:  # right / up → positive observation at the read
            value = score
        obs.append(Observation(axis=axis, value=value, tau=tau))

    loves = list(getattr(read, "namedLoveHits", None) or []) if gesture in ("right", "up") else []
    anti = list(getattr(read, "antiSignalHits", None) or []) if gesture == "left" else []
    primary = offending_axis if gesture == "left" else _strongest_axis(read, _weights(person))
    return Feedback(kind="swipe", observations=obs, direction=gesture,
                    antiSignalMentions=anti, namedLoves=loves,
                    archetypeTier=archetype_tier, primaryAxis=primary,
                    listingRef=Ref(id=getattr(listing, "id", None), schemaVersion="listing@1"))


def _strongest_axis(read: Any, weights: Dict[str, float]) -> Optional[str]:
    best, best_key = -1.0, None
    for axis in AXES:
        ar = read.axes.get(axis)
        if ar is None:
            continue
        val = float(ar.score) * float(weights.get(axis, 0.0))
        if val > best:
            best, best_key = val, axis
    return best_key


# ---------------------------------------------------------------------------
# §5.9 — the Taste-twin cold start (privacy-safe, calibrates away fast).
# ---------------------------------------------------------------------------

def broadest_london_twin(config: Dict[str, Any] = CONFIG) -> TasteTwin:
    """The homepage's cold-start prior when the stranger has volunteered nothing:
    the broadest London cohort — barely more than flat (σ 2.9, near σ0), so the
    clarity meter starts ~0 and the read is honestly the user's own after a swipe."""
    priors = {axis: TastePrior(mean=6.5 if axis in ("light_and_volume", "character_bones",
                                                     "outdoor_space") else 5.5, sigma=2.9)
              for axis in AXES}
    return TasteTwin(
        id="twin_ldn_broad_v1",
        cohortKey={"lifeStage": "unknown", "city": "London", "seedLoves": []},
        n=4200, priors=priors, antiSignalPriors=None,
        privacy=TwinPrivacy(kAnonymised=True, dpNoiseSigma=config["twin"]["dpNoiseSigma"],
                            noIndividualData=True, builtAt="2026-07-01T00:00:00Z"))


def build_twin(cohort_key: Dict[str, Any], priors: Dict[str, Tuple[float, float]], n: int,
               config: Dict[str, Any] = CONFIG) -> Optional[TasteTwin]:
    """Build a cohort twin, refusing below the k-anonymity floor (n≥minCohort,
    else no twin — a flat cold start, never a small-n leak, §5.9 rule 1)."""
    if n < config["twin"]["minCohort"]:
        return None
    floor = config["twin"]["priorSigmaFloor"]
    seed = "_".join(str(cohort_key.get(k, "")) for k in ("lifeStage", "city"))
    return TasteTwin(
        id="twin_%s_v1" % (seed.strip("_").replace(" ", "").lower() or "cohort"),
        cohortKey=cohort_key, n=n,
        priors={a: TastePrior(mean=m, sigma=max(s, floor)) for a, (m, s) in priors.items()},
        antiSignalPriors=None,
        privacy=TwinPrivacy(kAnonymised=True, dpNoiseSigma=config["twin"]["dpNoiseSigma"],
                            noIndividualData=True, builtAt="2026-07-01T00:00:00Z"))


def twin_weight(sigma_twin: float, tau: float, k: int) -> float:
    """The twin's residual weight in an axis belief after k observations of noise
    τ (§5.9): (1/σ²)/(1/σ² + k/τ²). Falls below 2% by ~4 obs at τ=0.8."""
    a = 1.0 / (sigma_twin ** 2)
    b = k / (tau ** 2)
    return a / (a + b)


# ---------------------------------------------------------------------------
# §5.10 — the reward loop: meter + milestones + discovery.
# ---------------------------------------------------------------------------

def reward_state(uncertainty: TasteUncertainty, person: Any, prev_clarity: float = 0.0,
                 milestones_hit: Optional[List[float]] = None,
                 config: Dict[str, Any] = CONFIG) -> RewardState:
    cl = uncertainty.overall.clarity0to1
    hit = list(milestones_hit or [])
    for m in config["rewards"]["clarityMilestones"]:
        if cl >= m and m not in hit:
            hit.append(m)
    return RewardState(
        personRef=Ref(id=person.id, schemaVersion="person@1"),
        clarity=cl, clarityDelta=_round2(cl - prev_clarity), milestonesHit=sorted(hit),
        streak=None, discoveries=[])


def is_discovery(score: float, novelty: float, config: Dict[str, Any] = CONFIG) -> bool:
    """The noise-inversion made felt (§5.10): a home that scores high yet sits
    outside the user's stated area/type — 'you'd never have filtered to this'."""
    return (score >= config["rewards"]["discoveryScoreFloor"]
            and novelty <= config["rewards"]["discoveryNoveltyMax"])


# --- Weight learning (spec 10 §2) ----------------------------------------------
# apply_feedback moves per-axis BELIEFS; this moves the WEIGHTS (importance) that
# taste_result actually scores on. Kept separate + opt-in (the profiler session
# calls it) so the original swipe path, golden fixture and suite are untouched.

_WL = {"lambdaBase": 0.25, "nHalf": 5.0}


def _project_simplex(v: List[float]) -> List[float]:
    """Euclidean projection onto {w >= 0, Σw = 1} (Duchi 2008) — turns the ridge
    solution into a valid weight vector."""
    u = sorted(v, reverse=True)
    css, rho = 0.0, 0
    for i in range(len(u)):
        css += u[i]
        if u[i] + (1.0 - css) / (i + 1) > 0:
            rho = i + 1
    theta = (sum(u[:rho]) - 1.0) / rho if rho else 0.0
    return [max(x - theta, 0.0) for x in v]


def _solve(A: List[List[float]], b: List[float]) -> List[float]:
    """Gauss-Jordan with partial pivoting for the small (8x8) normal system."""
    nn = len(A)
    M = [A[i][:] + [b[i]] for i in range(nn)]
    for c in range(nn):
        piv = max(range(c, nn), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            continue
        M[c], M[piv] = M[piv], M[c]
        d = M[c][c]
        M[c] = [x / d for x in M[c]]
        for r in range(nn):
            if r != c and M[r][c]:
                f = M[r][c]
                M[r] = [M[r][k] - f * M[c][k] for k in range(nn + 1)]
    return [M[i][nn] for i in range(nn)]


def learn_weights(history: List[Tuple[Dict[str, float], float]],
                  prior: Dict[str, float], n: Optional[int] = None,
                  tuning: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Refit the eight axis weights to best explain the reactions so far.

    ``history`` = ``[(reads, target)]`` where ``reads`` is the reacted home's
    per-axis scores (0-10) and ``target`` is the reaction's implied score (0-10,
    e.g. love 9 / dislike 2). Closed-form ridge fit toward the intro-seeded
    ``prior`` — minimise ``mean(x·w − t)² + λ‖w − prior‖²`` — then project onto the
    non-negative simplex so it is a valid weight vector for ``taste_result``. The
    prior's pull ``λ`` decays with ``n`` (early reactions nudge, ~15 lets the data
    lead an otherwise underdetermined eight-weight fit). Pure & deterministic."""
    t = dict(_WL)
    if tuning:
        t.update(tuning)
    p = [max(0.0, float(prior.get(a, 0.0))) for a in AXES]
    s = sum(p) or 1.0
    p = [x / s for x in p]
    if not history:
        return {a: _round2(p[i]) for i, a in enumerate(AXES)}
    n = len(history) if n is None else n
    d = len(AXES)
    X = [[float(reads.get(a, 5.0)) / 10.0 for a in AXES] for reads, _ in history]  # 0-1
    y = [float(tg) / 10.0 for _, tg in history]
    lam = t["lambdaBase"] * math.exp(-float(n) / t["nHalf"])   # strong prior early, data-led by ~15
    m = len(X)
    # normal equations for  mean(x·w − y)² + λ‖w − p‖²  →  (XᵀX/m + λI) w = Xᵀy/m + λp
    A = [[(lam if i == j else 0.0) for j in range(d)] for i in range(d)]
    b = [lam * p[i] for i in range(d)]
    for xi, yi in zip(X, y):
        for i in range(d):
            b[i] += xi[i] * yi / m
            Ai = A[i]
            for j in range(d):
                Ai[j] += xi[i] * xi[j] / m
    w = _project_simplex(_solve(A, b))
    return {a: _round2(w[i]) for i, a in enumerate(AXES)}


__all__ = [
    "CONFIG", "AXES", "Observation", "Feedback", "Receipt",
    "clarity", "seed_uncertainty", "apply_feedback", "expected_info_gain",
    "select_next_probe", "swipe_feedback", "broadest_london_twin", "build_twin",
    "twin_weight", "reward_state", "is_discovery", "learn_weights",
]
