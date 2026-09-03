"""U8 — the taste eval harness (03-engine §7). Verify-by-nature for the LLM.

The deterministic units (value, rules, composite, the taste *arithmetic*) are
verified by unit tests against the golden fixture. The Taste scorer's LLM
*judgement* cannot be pinned that way — it is verified by **measurement**: run
it over a calibration set of listings Finn actually rated and report the
error/agreement against his reactions. That is what produced the headline
numbers this project stands on:

    round 1 (text-only) : MAE 1.27, Spearman 0.77   (n=15)
    round 2 (text)      : MAE 1.64, Spearman 0.63   (n=11)   ← ablation floor
    round 2 (+images)   : MAE 1.35, Spearman 0.79   (n=11)   ← image lift

This module is the machinery that computes those numbers, plus the assertion
that they *reproduce* from the recorded calibration data
(``data/round{1,2}_scores.json``). It is pure stdlib (no numpy/scipy): MAE, a
tie-corrected Spearman (Pearson on average-ranks), within-band hit-rates, and an
ablation delta.

Two modes, one core:

* **Reproduce** (deterministic, what the tests run) — :func:`run_calibration`
  feeds the *recorded* predictions into :func:`evaluate` and checks they still
  give 1.27 / 0.77 / 1.35 / 0.79. This is the regression guard on the claim.
* **Live** (a real run) — :func:`evaluate_scorer` takes a ``scorer_fn(case) ->
  predicted`` (e.g. a wrapper round :func:`gaff_engine.taste.taste_result` with a
  live model) and re-measures the same metrics on fresh predictions, so the
  ablation can be re-checked on every model change (§5.1 stage 6 / §7).

The metrics are the contract; the recorded predictions are the fixture; a live
scorer is a drop-in that reuses the identical measurement.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# The recorded calibration ships with the package, resolved through
# gaff_engine.paths so it is found from an install as well as a checkout.
from gaff_engine import paths  # noqa: E402

DATA_DIR = paths.shipped_data_dir()

# The claims this harness guards (03-engine §7 / profile.json calibration).
CLAIMS = {
    "round1": {"mae": 1.27, "spearman": 0.77, "n": 15},
    "round2_final": {"mae": 1.35, "spearman": 0.79, "n": 11},
    "round2_text": {"mae": 1.64, "spearman": 0.63, "n": 11},
}
TOLERANCE = 0.02  # the claims are quoted to 2 dp; computed values must land within.


# ---------------------------------------------------------------------------
# Pure metrics — MAE, tie-corrected Spearman, within-band. No dependencies.
# ---------------------------------------------------------------------------

def _round(x: float, dp: int = 4) -> float:
    q = Decimal(1).scaleb(-dp)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def _key_order(k: str) -> Tuple[int, Any]:
    """Numeric-then-string sort key, so "2" sorts before "10" and mixed id
    schemes stay deterministic (the retest template and _paired must agree)."""
    try:
        return (0, int(k))
    except (TypeError, ValueError):
        return (1, str(k))


def _paired(predicted: Dict[str, float], actual: Dict[str, float]) -> List[str]:
    """The keys present in BOTH series, in a stable numeric-then-string order."""
    return sorted((k for k in predicted if k in actual), key=_key_order)


def mae(predicted: Dict[str, float], actual: Dict[str, float],
        keys: Optional[Sequence[str]] = None) -> float:
    """Mean absolute error over the paired keys."""
    keys = list(keys) if keys is not None else _paired(predicted, actual)
    if not keys:
        raise ValueError("mae needs at least one paired (predicted, actual) key")
    return _round(sum(abs(predicted[k] - actual[k]) for k in keys) / len(keys))


def average_ranks(values: Sequence[float]) -> List[float]:
    """1-based ranks with ties resolved to their average rank (fractional).
    Ascending: the smallest value gets rank 1. Tie-correct so Spearman matches
    the standard tie-corrected coefficient."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # average of the 1-based positions i+1..j+1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x: Sequence[float], y: Sequence[float]) -> float:
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = sum((a - mx) ** 2 for a in x) ** 0.5
    vy = sum((b - my) ** 2 for b in y) ** 0.5
    if vx == 0 or vy == 0:
        return 0.0  # a flat series has no rank signal
    return cov / (vx * vy)


def spearman(predicted: Dict[str, float], actual: Dict[str, float],
             keys: Optional[Sequence[str]] = None) -> float:
    """Tie-corrected Spearman rank correlation (Pearson on average-ranks)."""
    keys = list(keys) if keys is not None else _paired(predicted, actual)
    if len(keys) < 2:
        raise ValueError("spearman needs at least two paired keys")
    rp = average_ranks([predicted[k] for k in keys])
    ra = average_ranks([actual[k] for k in keys])
    return _round(_pearson(rp, ra))


def within(predicted: Dict[str, float], actual: Dict[str, float], band: float,
           keys: Optional[Sequence[str]] = None) -> Tuple[int, int]:
    """``(hits, n)`` where a hit is ``|predicted − actual| ≤ band``."""
    keys = list(keys) if keys is not None else _paired(predicted, actual)
    hits = sum(1 for k in keys if abs(predicted[k] - actual[k]) <= band)
    return hits, len(keys)


# ---------------------------------------------------------------------------
# Bootstrap confidence intervals. The calibration rounds are n=15 and n=11;
# a bare point estimate at that size overstates certainty, so every headline
# MAE/Spearman can now carry a percentile interval. Pure stdlib, seeded
# explicitly (random.Random(seed), never global state) so every run of the
# same data reproduces the same interval.
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile over an already-sorted list (the
    numpy 'linear' convention, so intervals match what a reader expects)."""
    if not sorted_vals:
        raise ValueError("percentile of an empty series")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(pos)
    if lo + 1 >= len(sorted_vals):
        return sorted_vals[-1]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[lo + 1] * frac


def bootstrap_ci(predicted: Dict[str, float], actual: Dict[str, float],
                 metric_fn: Callable[..., float], n_boot: int = 2000,
                 seed: int = 0, *, alpha: float = 0.05,
                 keys: Optional[Sequence[str]] = None) -> Tuple[float, float]:
    """Percentile bootstrap interval ``(lo, hi)`` for ``metric_fn`` at level
    ``1 − alpha``.

    ``metric_fn(predicted, actual, keys)`` — :func:`mae` and :func:`spearman`
    from this module fit directly. Resamples the paired keys with replacement;
    a resample the metric REFUSES (raises ValueError, e.g. Spearman over fewer
    than two paired keys) is skipped rather than faked. Note the distinction:
    a resample Spearman merely finds degenerate (all values tied on one side)
    is NOT skipped — it contributes 0.0 by this module's flat-series
    convention (see :func:`_pearson`), which is deliberate: skipping it would
    change every seeded interval already reported. Vanishingly rare at the
    calibration sizes (n=11-15) anyway. Deterministic for a given ``seed``:
    this interval is part of the reported claim, so it must reproduce
    exactly."""
    keys = list(keys) if keys is not None else _paired(predicted, actual)
    if not keys:
        raise ValueError("bootstrap_ci needs at least one paired key")
    rng = random.Random(seed)
    n = len(keys)
    stats: List[float] = []
    for _ in range(n_boot):
        sample = [keys[rng.randrange(n)] for _ in range(n)]
        try:
            stats.append(metric_fn(predicted, actual, sample))
        except ValueError:
            continue
    if not stats:
        raise ValueError("bootstrap_ci: the metric failed on every resample")
    stats.sort()
    return (_round(_percentile(stats, 100 * alpha / 2)),
            _round(_percentile(stats, 100 * (1 - alpha / 2))))


# ---------------------------------------------------------------------------
# Top-k metrics. Triage quality is the product's actual job: a model that
# puts the true top three in its top five is useful even at MAE 1.3, and MAE
# alone cannot see that. Tie rule (documented, no arbitrary tie-breaks): the
# "top k" of a series is the THRESHOLD set — every item whose value is >= the
# k-th largest value — so a tie at the boundary expands the set rather than
# being broken by key order. With no ties both sets have exactly k items and
# these reduce to the standard definitions.
# ---------------------------------------------------------------------------

def _top_set(series: Dict[str, float], keys: Sequence[str], k: int) -> set:
    if k < 1:
        raise ValueError("k must be >= 1")
    k = min(k, len(keys))
    threshold = sorted((series[key] for key in keys), reverse=True)[k - 1]
    return {key for key in keys if series[key] >= threshold}


def precision_at_k(predicted: Dict[str, float], actual: Dict[str, float],
                   k: int) -> float:
    """Fraction of the predicted top-k that is truly top-k (threshold-set tie
    rule above; denominator is the predicted set's size, so a predicted
    boundary tie is judged over everything the model put on top)."""
    keys = _paired(predicted, actual)
    if not keys:
        raise ValueError("precision_at_k needs at least one paired key")
    ptop = _top_set(predicted, keys, k)
    return _round(len(ptop & _top_set(actual, keys, k)) / len(ptop))


def recall_at_k(predicted: Dict[str, float], actual: Dict[str, float],
                k: int) -> float:
    """Fraction of the truly top-k items the predicted top-k recovered
    (same threshold-set tie rule; denominator is the actual set's size)."""
    keys = _paired(predicted, actual)
    if not keys:
        raise ValueError("recall_at_k needs at least one paired key")
    atop = _top_set(actual, keys, k)
    return _round(len(_top_set(predicted, keys, k) & atop) / len(atop))


def pairwise_preference_accuracy(predicted: Dict[str, float],
                                 actual: Dict[str, float],
                                 keys: Optional[Sequence[str]] = None) -> float:
    """Fraction of decided pairs the model orders the same way Finn did.

    Tie rule (documented): a pair TIED IN THE ACTUALS has no preference to
    recover and is excluded from the denominator; a PREDICTED tie on a decided
    pair scores 0.5 — chance credit, so expressed indifference is neither
    rewarded as a hit nor punished as a full miss."""
    keys = list(keys) if keys is not None else _paired(predicted, actual)
    decided, credit = 0, 0.0
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            da = actual[keys[i]] - actual[keys[j]]
            if da == 0:
                continue
            decided += 1
            dp = predicted[keys[i]] - predicted[keys[j]]
            if dp == 0:
                credit += 0.5
            elif (dp > 0) == (da > 0):
                credit += 1.0
    if decided == 0:
        raise ValueError(
            "pairwise_preference_accuracy: no decided pairs (all actuals tied)")
    return _round(credit / decided)


# ---------------------------------------------------------------------------
# The report object + the core evaluator.
# ---------------------------------------------------------------------------

@dataclass
class EvalReport:
    """The measurement for one predicted-vs-actual pass."""
    label: str = ""
    n: int = 0
    mae: float = 0.0
    spearman: float = 0.0
    within1_0: Tuple[int, int] = (0, 0)
    within1_5: Tuple[int, int] = (0, 0)
    worst: List[Tuple[str, float, float, float]] = field(default_factory=list)  # (key, pred, act, err)
    # Optional 95% bootstrap intervals (set when evaluate(..., ci=True)).
    # Small-n honesty: n=11-15 point estimates should not travel bare.
    mae_ci: Optional[Tuple[float, float]] = None
    spearman_ci: Optional[Tuple[float, float]] = None

    def meets(self, claim_mae: float, claim_spearman: float, tol: float = TOLERANCE) -> bool:
        """True if this report reproduces a claim within tolerance (MAE no worse,
        Spearman no worse, both to the quoted precision)."""
        return (self.mae <= claim_mae + tol) and (self.spearman >= claim_spearman - tol)

    def summary(self) -> str:
        w10h, w10n = self.within1_0
        w15h, w15n = self.within1_5
        base = ("%-14s n=%-2d  MAE %.4f  Spearman %.4f  within1.0 %d/%d  within1.5 %d/%d"
                % (self.label, self.n, self.mae, self.spearman, w10h, w10n, w15h, w15n))
        if self.mae_ci is not None and self.spearman_ci is not None:
            base += ("  MAE95 [%.2f, %.2f]  Sp95 [%.2f, %.2f]"
                     % (self.mae_ci + self.spearman_ci))
        return base


def evaluate(predicted: Dict[str, float], actual: Dict[str, float], *,
             label: str = "", labels: Optional[Dict[str, str]] = None,
             top_worst: int = 3, ci: bool = False, n_boot: int = 2000,
             seed: int = 0) -> EvalReport:
    """Measure a prediction series against actuals: MAE, tie-corrected Spearman,
    within-band hit-rates, and the ``top_worst`` largest-error items (named via
    ``labels`` when available). ``ci=True`` additionally attaches seeded 95%
    bootstrap intervals — additive only, so every existing asserted number is
    untouched by the upgrade."""
    keys = _paired(predicted, actual)
    errs = sorted(
        ((k, predicted[k], actual[k], abs(predicted[k] - actual[k])) for k in keys),
        key=lambda t: t[3], reverse=True)
    named = []
    for k, p, a, e in errs[:top_worst]:
        name = (labels or {}).get(k, "#%s" % k)
        named.append((name, p, a, _round(e, 2)))
    rep = EvalReport(
        label=label, n=len(keys),
        mae=mae(predicted, actual, keys),
        spearman=spearman(predicted, actual, keys),
        within1_0=within(predicted, actual, 1.0, keys),
        within1_5=within(predicted, actual, 1.5, keys),
        worst=named,
    )
    if ci:
        rep.mae_ci = bootstrap_ci(predicted, actual, mae,
                                  n_boot=n_boot, seed=seed, keys=keys)
        rep.spearman_ci = bootstrap_ci(predicted, actual, spearman,
                                       n_boot=n_boot, seed=seed, keys=keys)
    return rep


# ---------------------------------------------------------------------------
# Live mode — measure a scorer_fn over cases (a drop-in for a real taste run).
# ---------------------------------------------------------------------------

def evaluate_scorer(cases: Sequence[Any], scorer_fn: Callable[[Any], float],
                    actual: Dict[str, float], *, key_fn: Callable[[Any], str] = None,
                    label: str = "live", labels: Optional[Dict[str, str]] = None) -> EvalReport:
    """Run ``scorer_fn`` over ``cases`` and evaluate against ``actual``.

    ``key_fn(case) -> key`` maps a case to the actuals key (default: ``case["id"]``
    / ``case.id``). This is how a live taste run plugs in — e.g.
    ``scorer_fn = lambda c: taste_result(c.listing, person, live_model).score`` —
    reusing the identical metrics as the recorded reproduction."""
    def _default_key(c: Any) -> str:
        return str(getattr(c, "id", None) if not isinstance(c, dict) else c.get("id"))
    kf = key_fn or _default_key
    predicted = {kf(c): float(scorer_fn(c)) for c in cases}
    return evaluate(predicted, actual, label=label, labels=labels)


# ---------------------------------------------------------------------------
# Ablation — the round-2 text→image lift (§5.1 stage 6).
# ---------------------------------------------------------------------------

@dataclass
class AblationReport:
    text: EvalReport = None
    final: EvalReport = None

    @property
    def mae_lift(self) -> float:
        return _round(self.text.mae - self.final.mae)         # positive = images helped

    @property
    def spearman_lift(self) -> float:
        return _round(self.final.spearman - self.text.spearman)  # positive = images helped

    def summary(self) -> str:
        return ("image ablation: MAE %.4f → %.4f (lift %+.4f) · Spearman %.4f → %.4f (lift %+.4f)"
                % (self.text.mae, self.final.mae, self.mae_lift,
                   self.text.spearman, self.final.spearman, self.spearman_lift))


def ablation(text_prior: Dict[str, float], predicted_final: Dict[str, float],
             actual: Dict[str, float], *, labels: Optional[Dict[str, str]] = None,
             ci: bool = False, n_boot: int = 2000, seed: int = 0) -> AblationReport:
    """The text-only vs image-informed comparison on the same actuals."""
    kw = {"ci": ci, "n_boot": n_boot, "seed": seed}
    return AblationReport(
        text=evaluate(text_prior, actual, label="round2 text", labels=labels, **kw),
        final=evaluate(predicted_final, actual, label="round2 final", labels=labels, **kw),
    )


# ---------------------------------------------------------------------------
# Profile-leakage guard. The classic self-grading failure: scoring the model
# on listings the profile was BUILT from makes every calibration number a
# memory test, not a taste test. Cheap invariant, checked wherever a
# calibration set is loaded.
# ---------------------------------------------------------------------------

class ProfileLeakageError(ValueError):
    """A calibration set overlaps the listings the profile was built from."""


def _portal_shaped(s: str) -> bool:
    """True for an id that looks like a portal listing id (a long digit run).
    Ordinal calibration keys ("1".."15") are short; portal ids are 6+ digits.
    The distinction matters because two id namespaces that can never collide
    make a disjointness assertion vacuously true — a false pass."""
    return s.isdigit() and len(s) >= 6


def assert_disjoint(calibration_ids, profile_source_ids, *, context: str = "") -> None:
    """Raise :class:`ProfileLeakageError` if any id appears in BOTH sets.

    Ids are compared as strings (the score files key by str). Direction does
    not matter — leakage is leakage whichever set you name first."""
    overlap = sorted(set(map(str, calibration_ids)) & set(map(str, profile_source_ids)),
                     key=_key_order)
    if overlap:
        raise ProfileLeakageError(
            "profile leakage%s: %d listing id(s) appear in BOTH the calibration "
            "set and the profile's source listings: %s. A model scored on the "
            "listings its profile was built from is grading its own homework — "
            "remove the overlap from one side before trusting any number."
            % ((" (%s)" % context) if context else "", len(overlap), ", ".join(overlap)))


# ---------------------------------------------------------------------------
# Test-retest self-baseline (storage + comparison only; the rescoring is a
# Finn action). His own MAE against himself, ~2 weeks later and blind to his
# original scores, is the CEILING any model MAE should be read against: if
# self-consistency is 0.8-1.2, a model at 1.35 reads very differently than it
# does against an imagined perfect rater.
# ---------------------------------------------------------------------------

def save_retest_template(original: Dict[str, float], path: str, *,
                         label: str = "",
                         labels: Optional[Dict[str, str]] = None) -> str:
    """Write a sealed rescoring template for a past round: item ids with EMPTY
    score slots, deliberately excluding the original scores so the rescoring
    stays blind. JSON; conventionally under ``data/`` (lab tier), but the path
    is always the caller's explicit argument — never a hardcode.

    Refuses to overwrite: an existing file may already hold filled-in retest
    scores, and clobbering those silently would destroy the baseline."""
    if os.path.exists(path):
        raise FileExistsError(
            "retest template already exists at %s — it may hold filled-in "
            "scores; pick a new path or move it aside deliberately." % path)
    ids = sorted(original, key=_key_order)
    payload = {
        "kind": "gaff-retest-template",
        "label": label,
        "instructions": ("Score each item 0-10 from the listing alone. Do NOT "
                         "consult your original scores — the comparison is only "
                         "meaningful blind."),
        # The full id list, separate from the editable scores dict: the
        # template is filled in by hand-editing JSON, and a dropped line
        # DELETES a key rather than leaving it None — load_retest_scores
        # validates against this sealed list so that slip cannot silently
        # shrink the baseline.
        "ids": ids,
        "scores": {k: None for k in ids},
        "labels": labels or {},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return path


def load_retest_scores(path: str) -> Dict[str, float]:
    """Read a filled-in retest template back as ``{id: score}``.

    Refuses a partial template by naming the unscored ids — whether an entry
    was left ``null`` OR its line was deleted outright while hand-editing the
    JSON (validated against the template's sealed ``ids`` list, so a dropped
    key cannot silently shrink n and bias the self-baseline toward whichever
    items were rescored). An id in ``scores`` that is not in ``ids`` is
    refused too: a typo'd key would otherwise vanish in the comparison's key
    intersection."""
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    scores = blob.get("scores") or {}
    expected = list(map(str, blob.get("ids") or scores.keys()))
    missing = sorted((k for k in expected
                      if scores.get(k) is None), key=_key_order)
    if missing:
        raise ValueError(
            "retest template %s is incomplete — unscored item(s): %s"
            % (path, ", ".join(missing)))
    unexpected = sorted((k for k in scores if k not in set(expected)),
                        key=_key_order)
    if unexpected:
        raise ValueError(
            "retest template %s has score(s) for id(s) not in the template's "
            "ids list: %s — fix the key(s) rather than letting them drop out "
            "of the comparison." % (path, ", ".join(unexpected)))
    return {k: float(scores[k]) for k in expected}


def compare_retest(original: Dict[str, float], retest: Dict[str, float], *,
                   label: str = "test-retest") -> Dict[str, Any]:
    """Compare a blind rescoring against the original round.

    Returns self-MAE / self-Spearman framed as what they are: the rater's own
    repeatability, i.e. the ceiling for model MAE (a model cannot agree with
    Finn more than Finn agrees with himself).

    Refuses mismatched key sets rather than silently intersecting them: a
    retest missing items (or carrying strays) is a different experiment, and
    quietly shrinking n would bias the ceiling toward whichever items were
    rescored."""
    only_orig = sorted(set(original) - set(retest), key=_key_order)
    only_retest = sorted(set(retest) - set(original), key=_key_order)
    if only_orig or only_retest:
        parts = []
        if only_orig:
            parts.append("missing from the retest: %s" % ", ".join(only_orig))
        if only_retest:
            parts.append("in the retest only: %s" % ", ".join(only_retest))
        raise ValueError(
            "compare_retest: original and retest cover different items (%s) — "
            "a partial retest cannot stand in for the round it rescores."
            % "; ".join(parts))
    rep = evaluate(retest, original, label=label)
    return {
        "n": rep.n,
        "self_mae": rep.mae,
        "self_spearman": rep.spearman,
        "report": rep,
        "framing": ("self-consistency ceiling: self-MAE %.2f / self-Spearman "
                    "%.2f is the rater's own repeatability — read any model "
                    "MAE against this ceiling, not against zero."
                    % (rep.mae, rep.spearman)),
    }


# ---------------------------------------------------------------------------
# Recorded calibration — load the JSON score files + reproduce the claims.
# ---------------------------------------------------------------------------

def _load_json(name: str, data_dir: str) -> Dict[str, Any]:
    """Read one calibration file, preferring an explicit ``data_dir`` and
    otherwise letting paths resolve it across the user and shipped tiers."""
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        path = paths.data_file(name) or path
    with open(path) as f:
        return json.load(f)


# Best-effort item labels for the worst-error rows (round 1 complete from
# data/round1_results.md; round 2 names only the items its prose calls out).
ROUND1_LABELS = {
    "1": "Barbican triplex", "2": "Mayola Rd E5", "3": "Carrara Tower 35th",
    "4": "Gransden Ave BTR", "5": "Beck Rd E8", "6": "Sydner Rd N16",
    "7": "Foundry penthouse", "8": "Charlotte Rd warehouse", "9": "Blackstock Mews",
    "10": "Kingly penthouse", "11": "Cassland Rd £5,950", "12": "Hawksley Rd N16",
    "13": "Albion Terrace", "14": "Dunlace Rd E5", "15": "Cassland Rd £6,500",
}
ROUND2_LABELS = {"4": "East Bank", "11": "Killowen"}


def calibration_reports(data_dir: str = DATA_DIR, *, ci: bool = False,
                        n_boot: int = 2000, seed: int = 0,
                        profile_source_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Load the recorded scores and produce the round-1, round-2 and ablation
    reports (no assertion — just the measurement).

    ``ci=True`` attaches seeded 95% bootstrap intervals to every report —
    additive, so the asserted point numbers never move. Leakage guard: ids the
    profile was built from, declared either as a ``profile_source_ids`` field
    inside a score file or via the argument here, must be disjoint from that
    round's calibration ids or loading raises (see :func:`assert_disjoint`).

    The guard compares in BOTH id namespaces the data speaks: the ordinal
    calibration keys AND, via a ``listing_ids`` map (ordinal → portal listing
    id), the real listing ids behind them. Without that map an ordinal-keyed
    file could never collide with portal-shaped source ids, so a truthful
    declaration of genuine leakage would silently pass — that mismatch now
    raises instead of false-passing. The map and the profile's real source
    ids come from a score file's own fields or from the repo-local sidecar
    ``calibration_ids.json`` — a separate file because the score files ship
    in the public package (tools/build_public.py LOOSE_DATA) and real portal
    ids must not travel with them."""
    r1 = _load_json("round1_scores.json", data_dir)
    r2 = _load_json("round2_scores.json", data_dir)
    try:
        sidecar = _load_json("calibration_ids.json", data_dir)
    except (OSError, ValueError):
        sidecar = {}  # a public install has no sidecar; the in-file fields still apply
    for round_name, blob in (("round1", r1), ("round2", r2)):
        side = sidecar.get(round_name) or {} if isinstance(sidecar, dict) else {}
        declared = set(map(str, blob.get("profile_source_ids") or ()))
        declared |= set(map(str, side.get("profile_source_ids") or ()))
        declared |= set(map(str, profile_source_ids or ()))
        if not declared:
            continue
        cal_ids = set(map(str, blob["inferred_actual"].keys()))
        id_map = {str(k): str(v)
                  for src in (blob.get("listing_ids"), side.get("listing_ids"))
                  for k, v in (src or {}).items()}
        if id_map:
            unmapped = sorted(cal_ids - set(id_map), key=_key_order)
            if unmapped:
                raise ValueError(
                    "%s listing_ids map is incomplete — calibration item(s) "
                    "with no listing id: %s. Every item must be mappable or "
                    "the leakage guard cannot see real-id overlap."
                    % (round_name, ", ".join(unmapped)))
            # Compare in both namespaces: leakage is leakage whichever id
            # scheme the declaration used.
            cal_ids |= {id_map[k] for k in cal_ids & set(id_map)}
        elif (any(_portal_shaped(d) for d in declared)
              and not any(_portal_shaped(c) for c in cal_ids)):
            raise ValueError(
                "%s declares portal-shaped profile_source_ids but keys its "
                "calibration items by ordinals with no listing_ids map — the "
                "two namespaces can never collide, so the leakage guard would "
                "be vacuously true. Add a listing_ids (ordinal -> listing id) "
                "map to the score file or the calibration_ids.json sidecar."
                % round_name)
        assert_disjoint(cal_ids, declared,
                        context="%s calibration vs profile sources" % round_name)
    kw = {"ci": ci, "n_boot": n_boot, "seed": seed}
    round1 = evaluate(r1["predicted"], r1["inferred_actual"],
                      label="round1 text", labels=ROUND1_LABELS, **kw)
    abl = ablation(r2["text_prior"], r2["predicted_final"], r2["inferred_actual"],
                   labels=ROUND2_LABELS, **kw)
    return {"round1": round1, "round2_text": abl.text, "round2_final": abl.final,
            "ablation": abl}


def run_calibration(data_dir: str = DATA_DIR, *, check: bool = True,
                    ci: bool = False, n_boot: int = 2000, seed: int = 0,
                    profile_source_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Reproduce the headline calibration numbers from the recorded scores and
    (when ``check``) assert they still meet the published claims within
    :data:`TOLERANCE`. Returns the reports plus a ``passed``/``drift`` summary.

    This is the regression guard: if a data edit or a metric change ever moves
    MAE 1.27 / 0.77 / 1.35 / 0.79, the harness fails loudly."""
    reps = calibration_reports(data_dir, ci=ci, n_boot=n_boot, seed=seed,
                               profile_source_ids=profile_source_ids)
    checks = {
        "round1": reps["round1"].meets(CLAIMS["round1"]["mae"], CLAIMS["round1"]["spearman"]),
        "round2_final": reps["round2_final"].meets(
            CLAIMS["round2_final"]["mae"], CLAIMS["round2_final"]["spearman"]),
        "round2_text": reps["round2_text"].meets(
            CLAIMS["round2_text"]["mae"], CLAIMS["round2_text"]["spearman"]),
        "ablation_positive": reps["ablation"].mae_lift > 0 and reps["ablation"].spearman_lift > 0,
    }
    reps["checks"] = checks
    reps["passed"] = all(checks.values())
    if check and not reps["passed"]:
        failed = [k for k, ok in checks.items() if not ok]
        raise AssertionError("calibration drift — failed: %s" % ", ".join(failed))
    return reps


# ---------------------------------------------------------------------------
# CLI — print the calibration table (python3 -m gaff_engine.eval).
# ---------------------------------------------------------------------------

def _main() -> int:
    reps = run_calibration(check=False, ci=True)
    print("Taste calibration — reproduced from data/round{1,2}_scores.json")
    print("-" * 78)
    for key, claim_key in (("round1", "round1"), ("round2_text", "round2_text"),
                           ("round2_final", "round2_final")):
        rep = reps[key]
        claim = CLAIMS[claim_key]
        ok = rep.meets(claim["mae"], claim["spearman"])
        print("%s   [claim MAE %.2f / Spearman %.2f]  %s"
              % (rep.summary(), claim["mae"], claim["spearman"], "OK" if ok else "DRIFT"))
    print("-" * 78)
    print(reps["ablation"].summary())
    print("worst round-1 misses:")
    for name, p, a, e in reps["round1"].worst:
        print("   %-22s pred %.1f  actual %.1f  err %.1f" % (name, p, a, e))
    print("-" * 78)
    print("RESULT: %s" % ("PASS (all claims reproduce)" if reps["passed"] else "DRIFT"))
    return 0 if reps["passed"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())


__all__ = [
    "CLAIMS", "TOLERANCE", "EvalReport", "AblationReport", "ProfileLeakageError",
    "mae", "spearman", "average_ranks", "within", "evaluate", "evaluate_scorer",
    "ablation", "calibration_reports", "run_calibration",
    "bootstrap_ci", "precision_at_k", "recall_at_k", "pairwise_preference_accuracy",
    "assert_disjoint", "save_retest_template", "load_retest_scores", "compare_retest",
    "ROUND1_LABELS", "ROUND2_LABELS",
]
