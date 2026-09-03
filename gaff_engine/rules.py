"""U4 — the Rules scorer + gates engine (03-engine §5.3).

The *deterministic* layer of the Buy engine: no LLM, no I/O, pure evaluation of
a Search's declared ``gates[]`` (P1 §5.2) against a Listing's fields. It answers
two questions and nothing else:

1. **Is the Listing EXCLUDED?** — a failed *hard* Gate short-circuits the whole
   engine (03-engine §5.0 step 2): ``rules.excluded=True``, ``gatesPassed=False``,
   and at integration the composite is forced to 0 with taste/valueVerdict null
   (that nulling happens in the Mix layer; here we only set the marker + score).
2. **How well does a NON-excluded Listing meet the declared PREFERENCES?** — a
   deterministic 0-10 ``rules.score`` = ``clamp(hardGateBase + marginBonus −
   Σ softDocks, 0, 10)`` (§5.3), rewarding comfortable clearance of the hard
   gates and docking sub-floor *soft* gates (which flag, never exclude).

Authority: docs/spec/03-engine.md §5.3 (method + the De Beauvoir worked example
that lands ``score = 7.5``) and §5.0 ``engine.config@1.rules`` (``hardGateBase
8.0``, ``marginBonusCap 1.0``, ``softDockDefault −0.5`` — embedded in
:data:`CONFIG`). Learned *taste* rules (``new_build_cap`` etc.) live in §5.1's
Taste scorer, NOT here — this unit only handles declared Gates/Preferences.

Design — declarative + extensible (the task mandate):

* :data:`RESOLVERS` maps a Gate ``code`` to a small function that reads the
  Listing field the code names and reports ``(actual, verified)``; :data:`OPS`
  maps a Gate ``op`` to a comparison. A new gate kind = one entry in
  :data:`RESOLVERS` (reusing an existing operator) — no evaluator rewrite.
* A gate that reads a ``null``/``unknown`` field is *unverifiable*: it does NOT
  exclude (we never exclude on missing data — cf. §7.4 real-payload robustness),
  it passes as unverified and lowers ``rules`` confidence (§5.8).
* Reasons + flags belong on ``score.result`` in the full pipeline, so — mirroring
  :func:`gaff_engine.value.value_verdict` — :func:`rules_result` returns a
  schema-valid :class:`RulesResult` with ``.reasons`` / ``.flags`` / ``.confidence``
  attached as convenience attributes (the validator only reads declared fields).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from gaff_engine.schemas import (
    Flag, FlagCode, FlagKind, FlagSeverity, GateResult, Reason, RulesResult,
    SoftDock,
)

# ---------------------------------------------------------------------------
# CONFIG — the rules block of engine.config@1 (03-engine §5.0), inlined. The
# spec FIXES only base/cap/dock; the margin *shape* (how "comfortably clearing"
# a gate is turned into the bonus) and the rules-confidence curve are the
# implementer's, documented and tunable here — change a number, re-backtest,
# no code change (the memory rule: backtest offline before live).
# ---------------------------------------------------------------------------

CONFIG: Dict[str, Any] = {
    # Spec-fixed (§5.0 engine.config@1.rules).
    "hardGateBase": 8.0,          # a clean pass sits at 8.0 before margin/docks
    "marginBonusCap": 1.0,        # margin bonus never exceeds +1.0
    "softDockDefault": -0.5,      # each failed SOFT gate docks the score −0.5
    "scoreClamp": (0.0, 10.0),
    # Margin shape (implementer's). A hard gate is only "comfortably" cleared
    # once over-delivery exceeds the deadband — so De Beauvoir's modest margins
    # (2 beds vs 2, 1050 sqft vs 900 = +16.7% < 25%) contribute exactly 0.0,
    # reproducing the worked example's "8.0 + 0.0(modest margins) − 0.5 = 7.5".
    "marginComfortDeadband": 0.25,  # over-delivery below +25% earns nothing
    "marginBonusScale": 0.4,        # bonus per unit fractional-excess past the deadband
    # Rules confidence (§5.8): high by construction, lowered per unverifiable gate.
    "rulesConfidenceBase": 0.85,
    "rulesConfidenceNullPenalty": 0.10,
    "rulesConfidenceFloor": 0.50,
    "confidenceBands": {"high": 0.75, "medium": 0.5},
}

# Higher-is-better numeric gates that earn a margin bonus when a HARD gate is
# comfortably cleared (§5.3 "e.g. 1050 sqft vs a 900 floor, 2 baths vs a 2
# floor"). Price gates are NOT here — clearing a price ceiling is the Value
# scorer's job, not a "more is better" preference.
MARGIN_GATE_CODES = frozenset(
    {"min_beds", "min_baths", "min_sqft", "min_receptions", "lease_years_min"}
)

# Deterministic scan for outdoor space — listing@1 has no dedicated boolean, so
# the ``outdoor_present`` gate reads keyFeatures + description (a pure keyword
# match, not an LLM). Kept small and explicit.
OUTDOOR_KEYWORDS = (
    "garden", "terrace", "balcony", "patio", "roof terrace", "courtyard",
    "outdoor space", "decking", "veranda", "yard",
)

# Tenure values that carry no lease-term risk — a ``lease_years_min`` gate is a
# clean pass for these (no lease to be "short").
_NO_LEASE_TENURES = frozenset({"freehold", "share_of_freehold", "commonhold"})


# ---------------------------------------------------------------------------
# Deterministic numeric helpers (half-up rounding, mirroring composite/value).
# ---------------------------------------------------------------------------

def _round(x: float, dp: int) -> float:
    q = Decimal(1).scaleb(-dp)  # 10 ** -dp
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, x)))


# ---------------------------------------------------------------------------
# Duck-typed accessors — read a Listing / Search / Gate / dict / namespace
# alike, so the functions stay pure and tests can drive them with light dicts
# (the codebase style, cf. value._g / composite._score_weight).
# ---------------------------------------------------------------------------

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


def _enum_value(v: Any) -> Any:
    """Unwrap an Enum to its wire value; pass through str/None."""
    return getattr(v, "value", v)


def _num_field(obj: Any, *names: str) -> Tuple[Optional[float], bool]:
    v = _g(obj, *names)
    if v is None:
        return None, False
    try:
        return float(v), True
    except (TypeError, ValueError):
        return None, False


def _tenure(listing: Any) -> Optional[str]:
    v = _enum_value(_g(listing, "buy.tenure.type", "tenure.type", "tenureType", "tenure"))
    return str(v).lower() if v is not None else None


def _lease_years(listing: Any) -> Optional[int]:
    v = _g(listing, "buy.tenure.leaseYearsRemaining", "tenure.leaseYearsRemaining",
           "leaseYearsRemaining", "leaseYears")
    return int(v) if v is not None else None


def _has_outdoor(listing: Any) -> bool:
    bits: List[str] = []
    kf = _g(listing, "keyFeatures")
    if kf:
        bits.extend(str(x) for x in kf)
    desc = _g(listing, "description")
    if desc:
        bits.append(str(desc))
    blob = " ".join(bits).lower()
    return any(k in blob for k in OUTDOOR_KEYWORDS)


def _point_in_polygon(geo: Any, ring: List[Any]) -> bool:
    """Ray-casting point-in-polygon on the (lng, lat) plane. ``ring`` is a list
    of ``[lng, lat]`` vertices (open or closed both work)."""
    x = _g(geo, "lng")
    y = _g(geo, "lat")
    if x is None or y is None or not ring:
        return False
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------------
# The declarative tables — RESOLVERS (code -> reads the named Listing field)
# and OPS (op -> comparison). A new gate kind plugs in with one RESOLVERS entry.
# Each resolver returns ``(actual, verified)``; ``verified=False`` means the
# field was null/unknown so the gate cannot be enforced (does not exclude).
# ---------------------------------------------------------------------------

Resolver = Callable[[Any, Any], Tuple[Any, bool]]


def _resolve_price(listing: Any, search: Any) -> Tuple[Optional[int], bool]:
    v = _g(listing, "buy.price.amount", "price", "askingPrice")
    return (int(v), True) if v is not None else (None, False)


def _resolve_lease(listing: Any, search: Any) -> Tuple[Any, bool]:
    tenure = _tenure(listing)
    if tenure in _NO_LEASE_TENURES:
        return float("inf"), True        # no lease term -> any >= floor passes clean
    if tenure in (None, "unknown"):
        return None, False               # tenure unknown -> can't judge the lease
    years = _lease_years(listing)         # leasehold
    return (years, years is not None)


def _resolve_tenure(listing: Any, search: Any) -> Tuple[Optional[str], bool]:
    t = _tenure(listing)
    return (t, t is not None and t != "unknown")


def _resolve_outdoor(listing: Any, search: Any) -> Tuple[bool, bool]:
    return _has_outdoor(listing), True    # a determination is always made from text


def _resolve_geo(listing: Any, search: Any) -> Tuple[Optional[bool], bool]:
    geo = _g(listing, "geo")
    poly = _g(search, "area.polygon")
    if geo is None or not poly:
        return None, False
    return _point_in_polygon(geo, poly), True


RESOLVERS: Dict[str, Resolver] = {
    "min_beds":        lambda l, s: _num_field(l, "beds"),
    "min_baths":       lambda l, s: _num_field(l, "baths"),
    "min_sqft":        lambda l, s: _num_field(l, "sqft"),
    "min_receptions":  lambda l, s: _num_field(l, "receptions"),
    "max_price":       _resolve_price,
    "min_price":       _resolve_price,
    "lease_years_min": _resolve_lease,
    "tenure_in":       _resolve_tenure,
    "outdoor_present": _resolve_outdoor,
    "inside_polygon":  _resolve_geo,
}

OPS: Dict[str, Callable[[Any, Any], bool]] = {
    ">=": lambda a, e: a >= e,
    "<=": lambda a, e: a <= e,
    ">":  lambda a, e: a > e,
    "<":  lambda a, e: a < e,
    "==": lambda a, e: a == e,
    "!=": lambda a, e: a != e,
    "in": lambda a, e: a in e,
    # geo_within: the resolver already computed the boolean membership.
    "geo_within": lambda a, e: a is True,
}


# ---------------------------------------------------------------------------
# One evaluated gate (internal). ``verified`` distinguishes a genuine pass/fail
# from an unenforceable gate (null field). ``soft`` rides through from the Gate.
# ---------------------------------------------------------------------------

@dataclass
class GateEval:
    code: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    soft: bool
    verified: bool
    reason: str

    @property
    def hard_fail(self) -> bool:
        """A verified failure of a HARD gate — the only thing that excludes."""
        return (not self.passed) and (not self.soft)

    @property
    def soft_fail(self) -> bool:
        return (not self.passed) and self.soft


_UNIT = {
    "min_beds": "bed(s)", "min_baths": "bath(s)", "min_sqft": "sqft",
    "min_receptions": "reception(s)", "max_price": "GBP", "min_price": "GBP",
    "lease_years_min": "years",
}
_UNVERIFIED_NOTE = {
    "min_sqft": "no stated sqft",
    "min_receptions": "reception count not stated",
    "lease_years_min": "lease term not stated",
    "tenure_in": "tenure unknown",
    "inside_polygon": "no geo or search polygon",
    "max_price": "no stated price",
    "min_price": "no stated price",
}


def _fmt(v: Any) -> str:
    if isinstance(v, float) and v == float("inf"):
        return "no lease term"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _gate_reason(ge_code: str, op: str, expected: Any, actual: Any,
                 passed: bool, soft: bool) -> str:
    verdict = "pass" if passed else "FAIL"
    soft_tag = " (soft)" if soft else ""
    if ge_code == "outdoor_present":
        found = "outdoor space present" if actual else "no outdoor space found"
        return "outdoor_present: %s -> %s%s" % (found, verdict, soft_tag)
    if ge_code == "inside_polygon":
        where = "inside the search area" if actual else "outside the search area"
        return "inside_polygon: listing %s -> %s%s" % (where, verdict, soft_tag)
    if ge_code == "tenure_in":
        return "tenure_in: '%s' in %s -> %s%s" % (actual, list(expected), verdict, soft_tag)
    if ge_code == "lease_years_min":
        if actual == float("inf"):
            return "lease_years_min: no lease term (freehold/share of freehold) -> pass"
        return "lease_years_min: %s-yr lease vs required >= %s -> %s%s" % (
            _fmt(actual), _fmt(expected), verdict, soft_tag)
    unit = _UNIT.get(ge_code, "")
    u = (" " + unit) if unit else ""
    opword = {">=": "required >= ", "<=": "required <= ",
              "==": "required == "}.get(op, "required %s " % op)
    return "%s: %s%s vs %s%s%s -> %s%s" % (
        ge_code, _fmt(actual), u, opword, _fmt(expected), u, verdict, soft_tag)


def _unverified_reason(ge_code: str) -> str:
    note = _UNVERIFIED_NOTE.get(ge_code, "field not stated")
    return ("%s: %s -> gate unverified (rules confidence lowered), not excluded"
            % (ge_code, note))


# ---------------------------------------------------------------------------
# Gate evaluation.
# ---------------------------------------------------------------------------

def _evaluate_gate(listing: Any, search: Any, gate: Any) -> GateEval:
    code = _g(gate, "code")
    op = _g(gate, "op")
    expected = _g(gate, "value")
    soft = bool(_g(gate, "soft", default=False))

    resolver = RESOLVERS.get(code)
    if resolver is None:
        return GateEval(code, op, expected, None, passed=True, soft=soft, verified=False,
                        reason="gate '%s' has no evaluator -- not enforced (config note)" % code)
    op_fn = OPS.get(op)
    if op_fn is None:
        return GateEval(code, op, expected, None, passed=True, soft=soft, verified=False,
                        reason="operator '%s' unknown for gate '%s' -- not enforced" % (op, code))

    actual, verified = resolver(listing, search)
    if not verified or actual is None:
        return GateEval(code, op, expected, actual, passed=True, soft=soft, verified=False,
                        reason=_unverified_reason(code))

    try:
        passed = bool(op_fn(actual, expected))
    except TypeError:
        return GateEval(code, op, expected, actual, passed=True, soft=soft, verified=False,
                        reason=_unverified_reason(code))
    return GateEval(code, op, expected, actual, passed=passed, soft=soft, verified=True,
                    reason=_gate_reason(code, op, expected, actual, passed, soft))


def evaluate_gates(listing: Any, search: Any) -> List[GateEval]:
    """Evaluate every Gate the Search declares against the Listing (§5.3). Pure;
    the ordered list of :class:`GateEval` is the spine :func:`apply_gates`,
    :func:`rules_score` and :func:`rules_result` all read."""
    gates = _g(search, "gates", default=[]) or []
    return [_evaluate_gate(listing, search, gate) for gate in gates]


def _is_excluded(evals: List[GateEval]) -> bool:
    return any(e.hard_fail for e in evals)


# ---------------------------------------------------------------------------
# apply_gates — the hard-gate short-circuit primitive (§5.0 step 2).
# ---------------------------------------------------------------------------

def apply_gates(listing: Any, search: Any) -> Tuple[bool, List[str]]:
    """Evaluate the Search's HARD gates and decide exclusion (§5.0 step 2).

    Returns ``(excluded, gate_reasons)``: ``excluded`` is True iff any *hard*
    gate genuinely failed; ``gate_reasons`` is one plain-English string per
    failing hard gate, naming the gate and the Listing's value (a soft-gate
    failure never excludes, so it is not listed here -- it surfaces as a
    ``softDock`` + flag in :func:`rules_result`).
    """
    evals = evaluate_gates(listing, search)
    hard_fails = [e for e in evals if e.hard_fail]
    return (len(hard_fails) > 0, [e.reason for e in hard_fails])


# ---------------------------------------------------------------------------
# rules_score — the 0-10 preference-fit component (§5.3).
# ---------------------------------------------------------------------------

def _margin_bonus(evals: List[GateEval]) -> float:
    """Reward comfortably clearing the HARD gates (§5.3), capped at
    ``marginBonusCap``. Only higher-is-better numeric gates (:data:`MARGIN_GATE_CODES`)
    that are hard, verified and passed contribute; each contributes its
    fractional over-delivery beyond the comfort deadband. De Beauvoir's modest
    margins fall under the deadband -> bonus 0.0 (the worked example)."""
    total = 0.0
    for e in evals:
        if e.code not in MARGIN_GATE_CODES:
            continue
        if e.soft or not e.passed or not e.verified:
            continue
        floor, actual = e.expected, e.actual
        if actual == float("inf") or not isinstance(floor, (int, float)) or floor <= 0:
            continue
        frac = (float(actual) - float(floor)) / float(floor)
        total += max(0.0, frac - CONFIG["marginComfortDeadband"])
    return _clamp(total * CONFIG["marginBonusScale"], 0.0, CONFIG["marginBonusCap"])


def _soft_docks(evals: List[GateEval]) -> List[SoftDock]:
    return [SoftDock(rule="%s (soft)" % e.code, delta=CONFIG["softDockDefault"])
            for e in evals if e.soft_fail]


def rules_score(listing: Any, search: Any) -> float:
    """The 0-10 rules component (§5.3): ``clamp(hardGateBase + marginBonus −
    Σ softDocks, 0, 10)``, 1 dp. An excluded Listing scores 0. Reproduces the
    golden De Beauvoir ``7.5`` (8.0 + 0.0 − 0.5)."""
    evals = evaluate_gates(listing, search)
    if _is_excluded(evals):
        return 0.0
    raw = (CONFIG["hardGateBase"]
           + _margin_bonus(evals)
           + sum(d.delta for d in _soft_docks(evals)))
    return _round(_clamp(raw, *CONFIG["scoreClamp"]), 1)


# ---------------------------------------------------------------------------
# rules confidence (§5.8) — high by construction, lowered per unverifiable gate.
# ---------------------------------------------------------------------------

def confidence_band(scalar: float) -> str:
    b = CONFIG["confidenceBands"]
    if scalar >= b["high"]:
        return "high"
    if scalar >= b["medium"]:
        return "medium"
    return "low"


def rules_confidence(evals: List[GateEval]) -> float:
    """Deterministic evaluation -> ``rulesConfidenceBase`` (0.85), lowered
    ``rulesConfidenceNullPenalty`` per gate that read a null/unknown field
    (§5.8), floored at ``rulesConfidenceFloor``."""
    unverified = sum(1 for e in evals if not e.verified)
    if unverified == 0:
        return CONFIG["rulesConfidenceBase"]
    scalar = CONFIG["rulesConfidenceBase"] - CONFIG["rulesConfidenceNullPenalty"] * unverified
    return _round(_clamp(scalar, CONFIG["rulesConfidenceFloor"], CONFIG["rulesConfidenceBase"]), 2)


# ---------------------------------------------------------------------------
# reasons + flags (belong on score.result; attached to the result for the unit's
# consumers, cf. value.value_verdict's .reasons — the validator ignores them).
# Flags use ONLY valid FlagCode members (a closed-but-additive U1 enum): the one
# rules gate that maps cleanly is the short lease -> SHORT_LEASE. Other advisories
# (e.g. "no stated sqft") surface as reasons, since FlagCode has no slot and this
# unit must not redefine U1's schema.
# ---------------------------------------------------------------------------

def _reasons(evals: List[GateEval], excluded: bool) -> List[Reason]:
    out: List[Reason] = []
    if excluded:
        for e in evals:
            if e.hard_fail:
                out.append(Reason(scorer="rules", polarity="−", text=e.reason))
        return out
    for e in evals:
        if e.soft_fail:
            out.append(Reason(scorer="rules", polarity="−",
                              text=e.reason + " — flagged and docked -0.5, not excluded."))
    for e in evals:
        if not e.verified:
            out.append(Reason(scorer="rules", polarity="−", text=e.reason))
    if _margin_bonus(evals) > 0:
        out.append(Reason(scorer="rules", polarity="+",
                          text="Comfortably clears the hard gates — a margin bonus lifts the rules score."))
    if not out:
        out.append(Reason(scorer="rules", polarity="+",
                          text="All declared gates pass; nothing to flag."))
    return out


def _flags(evals: List[GateEval]) -> List[Flag]:
    out: List[Flag] = []
    for e in evals:
        if e.code == "lease_years_min" and not e.passed and e.verified:
            years = _fmt(e.actual)
            out.append(Flag(
                code=FlagCode.SHORT_LEASE, severity=FlagSeverity.SERIOUS,
                text=("%s-yr lease — under the %s-yr floor and the sub-90 "
                      "lending/marriage-value line. Budget an extension."
                      % (years, _fmt(e.expected))),
                kind=FlagKind.LISTING, source="tenure.leaseYearsRemaining"))
    return out


# ---------------------------------------------------------------------------
# rules_result — the top-level RulesResult (§5.3).
# ---------------------------------------------------------------------------

def rules_result(listing: Any, search: Any) -> RulesResult:
    """Produce the schema-valid :class:`RulesResult` for a Listing under a Search
    (§5.3): ``score``, ``gatesPassed``, ``excluded``, ``gateResults[]`` (one per
    declared gate) and ``softDocks[]``. Plain-English ``.reasons``, ``.flags`` and
    a ``.confidence`` scalar are attached (they live on ``score.result`` in the
    full pipeline; the validator reads only the declared fields, cf.
    :func:`gaff_engine.value.value_verdict`).

    On a hard-gate exclusion: ``excluded=True``, ``gatesPassed=False``,
    ``score=0`` (per §5.0 step 2 the Mix then forces composite 0 and nulls
    taste/valueVerdict -- done at integration, not here)."""
    evals = evaluate_gates(listing, search)
    excluded = _is_excluded(evals)

    gate_results = [
        GateResult(code=e.code, passed=e.passed, soft=(True if e.soft else None))
        for e in evals
    ]
    soft_docks = _soft_docks(evals)

    if excluded:
        score = 0.0
    else:
        raw = (CONFIG["hardGateBase"] + _margin_bonus(evals)
               + sum(d.delta for d in soft_docks))
        score = _round(_clamp(raw, *CONFIG["scoreClamp"]), 1)

    result = RulesResult(
        score=score,
        gatesPassed=(not excluded),
        excluded=excluded,
        gateResults=gate_results,
        softDocks=(soft_docks or None),
    )
    # Convenience attributes (not RulesResult schema fields; see module docstring).
    result.reasons = _reasons(evals, excluded)
    result.flags = _flags(evals)
    result.confidence = rules_confidence(evals)
    return result


__all__ = [
    "CONFIG", "MARGIN_GATE_CODES", "OUTDOOR_KEYWORDS", "RESOLVERS", "OPS",
    "GateEval", "evaluate_gates", "apply_gates", "rules_score",
    "rules_confidence", "confidence_band", "rules_result",
]
