"""The deterministic composite combiner (U5's math, needed by U1's oracle).

Two PURE functions — no LLM, no I/O, no globals — that reproduce the golden
De Beauvoir arithmetic from 03-engine §5.1 / §5.6 and 01-domain §5.5b:

* ``taste_score(axis_breakdown, adjustments)`` — the weighted-average taste
  base plus signed adjustments, clamped to [0, 10] and rounded to 1 dp
  (03-engine §5.1 stages 2 + 5):

      base  = Σ(axisScore · weight) / Σ(weight)          # over all eight axes
      score = round(clamp(base + Σ adjustments, 0, 10), 1)

* ``composite(taste, rules, value, mix)`` — the Scorer-Mix headline
  (03-engine §5.6, verbatim the 01-domain §5.5b definition):

      composite = round((taste·mixTaste + rules·mixRules + value·mixValue) / 100, 1)

Golden values these reproduce exactly:
    taste_score(De Beauvoir 8 axes, +0.30) == 8.2   # 426.5/54.0 = 7.90 -> 8.20
    composite(8.2, 7.5, 7.2, (55, 20, 25)) == 7.8   # 781/100 = 7.81 -> 7.8

Rounding uses Decimal ROUND_HALF_UP on the value's shortest decimal string, so
"round to 1 dp" is deterministic and platform-independent (no banker's-rounding
surprise). The golden numbers do not sit on a half-way boundary, so the choice
does not change them; it is chosen for reproducibility.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Sequence, Tuple, Union

Number = Union[int, float]


def _round1(x: Number) -> float:
    """Round to one decimal place, half-up, deterministically."""
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _clamp(x: Number, lo: Number = 0.0, hi: Number = 10.0) -> float:
    return float(min(hi, max(lo, x)))


def _score_weight(row: Any) -> Tuple[float, float]:
    """Extract (score, weight) from an AxisBreakdown, a mapping, or a pair."""
    if hasattr(row, "score") and hasattr(row, "weight"):
        return float(row.score), float(row.weight)
    if isinstance(row, dict):
        return float(row["score"]), float(row["weight"])
    if isinstance(row, (tuple, list)) and len(row) >= 2:
        return float(row[0]), float(row[1])
    raise TypeError("axis row must expose .score/.weight, be a dict, or a (score, weight) pair")


def _sum_adjustments(adjustments: Any) -> float:
    """Sum adjustment deltas. Accepts a scalar, or an iterable of scalars /
    TasteAdjustment objects (``.delta``) / mappings (``{"delta": ...}``)."""
    if adjustments is None:
        return 0.0
    if isinstance(adjustments, (int, float)) and not isinstance(adjustments, bool):
        return float(adjustments)
    total = 0.0
    for a in adjustments:
        if hasattr(a, "delta"):
            total += float(a.delta)
        elif isinstance(a, dict):
            total += float(a["delta"])
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            total += float(a)
        else:
            raise TypeError("adjustment must be a number, expose .delta, or be a {'delta': ...} mapping")
    return total


def _mix_weights(mix: Any) -> Tuple[float, float, float]:
    """Extract (taste, rules, value) weights from a ScorerMix, mapping, or triple."""
    if hasattr(mix, "taste") and hasattr(mix, "rules") and hasattr(mix, "value"):
        return float(mix.taste), float(mix.rules), float(mix.value)
    if isinstance(mix, dict):
        return float(mix["taste"]), float(mix["rules"]), float(mix["value"])
    if isinstance(mix, (tuple, list)) and len(mix) == 3:
        return float(mix[0]), float(mix[1]), float(mix[2])
    raise TypeError("mix must expose .taste/.rules/.value, be a dict, or a (taste, rules, value) triple")


def taste_score(axis_breakdown: Iterable[Any], adjustments: Any = 0.0) -> float:
    """Weighted taste base over the axis breakdown, plus adjustments, clamped
    to [0, 10] and rounded to 1 dp. ``adjustments`` may be a scalar (e.g.
    ``+0.30``) or an iterable of deltas / TasteAdjustment rows.

    Reproduces the golden 8.2 from the eight De Beauvoir axes and the +0.30
    named-love adjustment.
    """
    total_weight = 0.0
    weighted_sum = 0.0
    n = 0
    for row in axis_breakdown:
        score, weight = _score_weight(row)
        weighted_sum += score * weight
        total_weight += weight
        n += 1
    if n == 0 or total_weight == 0:
        raise ValueError("taste_score requires at least one axis row with non-zero total weight")
    base = weighted_sum / total_weight
    adjusted = base + _sum_adjustments(adjustments)
    return _round1(_clamp(adjusted, 0.0, 10.0))


def composite(taste: Number, rules: Number, value: Number, mix: Any) -> float:
    """The Scorer-Mix composite, rounded to 1 dp. ``mix`` is a ScorerMix, a
    ``{"taste","rules","value"}`` mapping, or a ``(taste, rules, value)`` triple
    of weights summing to 100.

    Reproduces the golden 7.8 from (8.2, 7.5, 7.2) at the Buy mix 55/20/25.
    """
    mt, mr, mv = _mix_weights(mix)
    weighted = (taste * mt + rules * mr + value * mv) / 100.0
    return _round1(weighted)
