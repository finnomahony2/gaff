"""gaff_engine/preference.py — the Pairwise-Bayesian taste model (spec 11).

Learns a person's SIGNED axis weights from pairwise "A or B?" duels, carrying a full
Gaussian posterior (Laplace) so we get three things the point-estimate signed ridge
cannot: principled next-question selection (expected information gain), an honest
confidence readout, and a warm-start prior from the intro. One GAI interaction term
(character x period_authentic) lets a person love architectural character while
disliking period-fussiness — the Shreiber fix.

Pure, deterministic, stdlib-only. ADDITIVE to the profiler: does not touch learn_fit /
learn_weights or the golden Buy path. See docs/spec/11-preference-model.md.
"""
import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from gaff_engine.swipe import AXES, _solve

# Location-blind calibration axes (mirrors profiler._CALIB; kept local to avoid a cycle).
_LOCATION = ("street_scene", "station_proximity")
BASE_AXES: List[str] = [a for a in AXES if a not in _LOCATION]      # 6 axes
INTERACTION = "character_bones*period_authentic"
FEATURES: List[str] = BASE_AXES + [INTERACTION]                    # fs1 feature order
FEATURESET = "fs1"
DIM = len(FEATURES)                                                # 7

# Prior defaults (spec 5.2), on the [0,1] feature scale. Weakly-informative: the pairwise
# signal is thin (1 bit/duel over correlated photo-axes), so an over-tight prior stalls
# recovery. S_OPEN stays loose enough that ~20 duels lead; S_TIGHT only nudges.
_M0, _DECAY, _A0 = 0.6, 0.6, 0.5
_S_TIGHT, _S_OPEN, _S_MID = 1.0, 2.5, 1.5


# --------------------------------------------------------------------------- features
def feature_map(reads: Dict[str, float], period_authentic: bool = False) -> List[float]:
    """A home's per-axis vision reads (0-10) + its period_authentic flag -> phi (fs1)."""
    v = [float(reads.get(a, 5.0)) / 10.0 for a in BASE_AXES]
    char = float(reads.get("character_bones", 5.0)) / 10.0
    v.append(char * (1.0 if period_authentic else 0.0))
    return v


# ------------------------------------------------------------------------------ prior
def intro_to_prior(parse: Optional[Dict[str, Any]] = None,
                   anti_axis_map: Optional[Dict[str, str]] = None
                   ) -> Tuple[List[float], List[float]]:
    """parse_search_intro output -> (mu0, sigma0) over FEATURES (diagonal prior).

    tastePriorities (ranked) set decreasing positive means on the axes the person
    cares about, tighter variance where they spoke. antiSignals only push a negative
    mean when a caller-supplied map resolves a phrase to an axis (approximate by
    nature; default = leave it to the data). The interaction gets an open-ish prior."""
    mu0 = [0.0] * DIM
    sig = [_S_OPEN] * DIM
    sig[FEATURES.index(INTERACTION)] = _S_MID
    if not parse:
        return mu0, sig
    prios = [a for a in (parse.get("tastePriorities") or []) if a in BASE_AXES]
    for rank, a in enumerate(prios):
        i = FEATURES.index(a)
        mu0[i] = _M0 * (_DECAY ** rank)
        sig[i] = _S_TIGHT
    if anti_axis_map:
        for phrase in (parse.get("antiSignals") or []):
            ax = anti_axis_map.get(str(phrase).strip().lower())
            if ax in BASE_AXES:
                i = FEATURES.index(ax)
                mu0[i] = -_A0
                sig[i] = _S_TIGHT
    return mu0, sig


# --------------------------------------------------------------------------- posterior
def _sigmoid(t: float) -> float:
    if t >= 0:
        z = math.exp(-t)
        return 1.0 / (1.0 + z)
    z = math.exp(t)
    return z / (1.0 + z)


def _softplus(t: float) -> float:
    return t + math.log1p(math.exp(-t)) if t > 0 else math.log1p(math.exp(t))


def _neg_log_post(w: List[float], obs: Sequence[Tuple[List[float], int]],
                  mu0: List[float], prec0: List[float]) -> float:
    p = len(w)
    L = 0.5 * sum(prec0[i] * (w[i] - mu0[i]) ** 2 for i in range(p))
    for z, y in obs:
        u = sum(w[k] * z[k] for k in range(p))
        L += _softplus(-u) if y == 1 else _softplus(u)   # -log sig(u) / -log(1-sig(u))
    return L


def _hessian(w: List[float], obs: Sequence[Tuple[List[float], int]],
             prec0: List[float]) -> List[List[float]]:
    p = len(w)
    H = [[(prec0[i] if i == j else 0.0) for j in range(p)] for i in range(p)]
    for z, y in obs:
        u = sum(w[k] * z[k] for k in range(p))
        s = _sigmoid(u)
        wgt = s * (1.0 - s)
        if wgt == 0.0:
            continue
        for i in range(p):
            if z[i] == 0.0:
                continue
            wi = wgt * z[i]
            row = H[i]
            for j in range(p):
                row[j] += wi * z[j]
    return H


def _invert(H: List[List[float]]) -> List[List[float]]:
    """H^-1 via _solve against each identity column, then symmetrised."""
    p = len(H)
    cols = []
    for j in range(p):
        e = [1.0 if i == j else 0.0 for i in range(p)]
        cols.append(_solve(H, e))
    inv = [[cols[j][i] for j in range(p)] for i in range(p)]
    return [[0.5 * (inv[i][j] + inv[j][i]) for j in range(p)] for i in range(p)]


def fit_posterior(obs: Sequence[Tuple[List[float], int]],
                  mu0: List[float], sigma0: List[float],
                  max_iter: int = 25, tol: float = 1e-8
                  ) -> Tuple[List[float], List[List[float]], int, float, bool]:
    """Newton MAP + Laplace covariance of a Bayesian logistic (Bradley-Terry) fit.

    obs = [(z, y)] with z = phi_A - phi_B and y in {0,1} (1 = A preferred). Convex
    (Gaussian precision + logistic Hessian), so the MAP is unique; the prior precision
    keeps H invertible even on separable data. Returns (w, cov, nObs, logPost, converged)."""
    p = len(mu0)
    prec0 = [1.0 / (s * s) for s in sigma0]
    w = list(mu0)
    converged = False
    for _ in range(max_iter):
        g = [prec0[i] * (w[i] - mu0[i]) for i in range(p)]
        for z, y in obs:
            u = sum(w[k] * z[k] for k in range(p))
            r = y - _sigmoid(u)
            for i in range(p):
                g[i] -= r * z[i]
        if max(abs(x) for x in g) < tol:
            converged = True
            break
        H = _hessian(w, obs, prec0)
        delta = _solve(H, g)
        L0 = _neg_log_post(w, obs, mu0, prec0)
        step = 1.0
        while step > 1e-6:
            wt = [w[i] - step * delta[i] for i in range(p)]
            if _neg_log_post(wt, obs, mu0, prec0) <= L0 + 1e-12:
                break
            step *= 0.5
        w = [w[i] - step * delta[i] for i in range(p)]
    cov = _invert(_hessian(w, obs, prec0))
    return w, cov, len(obs), -_neg_log_post(w, obs, mu0, prec0), converged


# ----------------------------------------------------------------------------- scoring
def utility(w: List[float], phi: List[float]) -> float:
    return sum(w[k] * phi[k] for k in range(len(w)))


def calibrate_squash(utils: Sequence[float]) -> Tuple[float, float]:
    """(ubar, scale) for the 0-10 squash — mean + a MAD-robust scale over the pool."""
    n = len(utils)
    if not n:
        return 0.0, 1.0
    ubar = sum(utils) / n
    srt = sorted(utils)
    med = srt[n // 2]
    mad = sorted(abs(u - med) for u in utils)[n // 2]
    return ubar, max(1e-6, 1.4826 * mad)


def score(u: float, ubar: float, s: float) -> float:
    return 10.0 * _sigmoid((u - ubar) / s)


def home_score(w: List[float], cov: List[List[float]], phi: List[float],
               ubar: float, s: float) -> Tuple[float, float]:
    """(0-10 score, +/- band) — the band is the posterior predictive sd via delta method."""
    p = len(phi)
    u = utility(w, phi)
    m = (u - ubar) / s
    nu = 0.0
    for i in range(p):
        pi = phi[i]
        if pi == 0.0:
            continue
        for j in range(p):
            nu += pi * cov[i][j] * phi[j]
    nu = max(0.0, nu)
    dsig = _sigmoid(m) * (1.0 - _sigmoid(m))
    return 10.0 * _sigmoid(m), 10.0 * dsig * math.sqrt(nu) / s


def axis_confidence(cov: List[List[float]], sigma0: List[float],
                    w: List[float]) -> Dict[str, Any]:
    per = {}
    for i, f in enumerate(FEATURES):
        sd = math.sqrt(max(0.0, cov[i][i]))
        resolved = max(0.0, min(1.0, 1.0 - sd / sigma0[i])) if sigma0[i] > 0 else 0.0
        per[f] = {"sd": sd, "resolved0to1": resolved,
                  "state": "resolved" if resolved >= 0.6 else "learning"}
    denom = sum(abs(x) for x in w) or 1.0
    overall = sum(abs(w[i]) * per[FEATURES[i]]["resolved0to1"] for i in range(len(w))) / denom
    least = sorted(FEATURES, key=lambda f: per[f]["resolved0to1"])[:2]
    return {"perAxis": per, "overall0to1": overall, "leastResolved": least}


# -------------------------------------------------------------------- active selection
def acquisition(w: List[float], cov: List[List[float]], z: List[float]) -> float:
    """a(z) = sigma(u)(1-sigma(u)) * z^T cov z (spec 6): outcome-uncertain AND informative."""
    u = utility(w, z)
    s = _sigmoid(u)
    p = len(z)
    zcz = 0.0
    for i in range(p):
        zi = z[i]
        if zi == 0.0:
            continue
        for j in range(p):
            zcz += zi * cov[i][j] * z[j]
    return s * (1.0 - s) * max(0.0, zcz)


def select_pair(cand_ids: Sequence[str], phi_by_id: Dict[str, List[float]],
                w: List[float], cov: List[List[float]],
                seen: Optional[Set[str]] = None, min_z: float = 0.15,
                comparable: Optional[Any] = None, cap: int = 200) -> Optional[Dict[str, Any]]:
    """The next duel: argmax a(z) (spec 6) over COMPARABLE unseen pairs. `comparable(a,b)`
    keeps the two homes in a similar tier/size, so the choice turns on STYLE not scale
    (a 1-bed flat vs a mansion teaches nothing about taste). Relaxes the predicate only
    if it would otherwise leave no admissible pair."""
    seen = seen or set()
    ids = sorted(i for i in cand_ids if i not in seen)[:cap]     # bound the O(n^2) scan
    best = None
    for ai in range(len(ids)):
        a = ids[ai]
        pa = phi_by_id[a]
        for bi in range(ai + 1, len(ids)):
            b = ids[bi]
            if comparable is not None and not comparable(a, b):
                continue
            pb = phi_by_id[b]
            z = [pa[k] - pb[k] for k in range(DIM)]
            if math.sqrt(sum(t * t for t in z)) < min_z:
                continue
            gain = acquisition(w, cov, z)
            if best is None or gain > best["expectedInfoGain"]:
                focus = FEATURES[max(range(DIM), key=lambda k: abs(z[k]))]
                best = {"aId": a, "bId": b, "axisFocus": focus, "expectedInfoGain": gain}
    if best is None and comparable is not None:                  # nothing comparable — relax
        return select_pair(cand_ids, phi_by_id, w, cov, seen=seen, min_z=min_z, comparable=None, cap=cap)
    return best
