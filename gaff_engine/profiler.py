"""spec 10 — the adaptive profiler engine.

Calibrate a taste from ~15 contrastive reactions on the vision-graded cohort,
LEARNING THE WEIGHTS as it goes (spec 10 §2, the fix the first live run demanded),
picking the most-informative next home (§3) and naming the variable it tests, then
measure the result on blind-rated held-out homes. A NEW copy alongside the second-rater
trial (rater_trial.py, lab code) — the original is deliberately left untouched
for comparison.

The calibration reads come from the cohort's cached VISION reads
(data/cohort_vision.json), so a whole session makes ZERO live model calls and is
deterministic for tests.
"""
from __future__ import annotations

import json
import os
import random
from typing import Any, Dict, List, Optional, Tuple

from gaff_engine.elicit import person_from_answers
from gaff_engine.eval import evaluate
from gaff_engine.ingest import normalise
from gaff_engine.swipe import (
    AXES, apply_feedback, broadest_london_twin, learn_weights, seed_uncertainty,
    swipe_feedback, _write_anti_signal, _solve,
)
from gaff_engine.taste import taste_result
from gaff_engine.taste_live import LiveTasteModel, replay_model
from gaff_engine import preference as pref

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COHORT = os.path.join(_ROOT, "data", "profiling_cohort.json")
VISION = os.path.join(_ROOT, "data", "cohort_vision.json")
TODAY = "2026-07-15"

_GESTURE = {"love": "up", "like": "right", "meh": "left", "dislike": "left"}
_TARGET = {"love": 9.0, "like": 7.0, "meh": 4.5, "dislike": 2.0}
# vision reads carry 7 axes; station_proximity is not judged from photos.
_VISION_AXES = ["design_finish", "character_bones", "light_and_volume",
                "width_proportion_flow", "raw_size_threshold", "outdoor_space",
                "street_scene"]
# calibration is LOCATION-BLIND (spec 10 §4): street/station belong to the later
# area step, and we can't reliably show a street photo for every home, so we must
# not learn a taste for them here.
_LOCATION = ("street_scene", "station_proximity")
_CALIB = [a for a in AXES if a not in _LOCATION]   # the axes calibration learns
_AXIS_LABEL = {"design_finish": "the finish", "character_bones": "the period character",
               "light_and_volume": "light and volume", "width_proportion_flow": "the proportions",
               "raw_size_threshold": "the size", "outdoor_space": "outdoor space",
               "street_scene": "the street"}
_MATCH_LABEL = {"design_finish": "the finish", "character_bones": "the period character",
                "light_and_volume": "the light", "width_proportion_flow": "the proportions",
                "raw_size_threshold": "the size", "outdoor_space": "the outdoor space"}
_DETAIL_LABEL = {"design_finish": "Design & finish", "character_bones": "Period character",
                 "light_and_volume": "Light & volume", "width_proportion_flow": "Proportions & flow",
                 "raw_size_threshold": "Size", "outdoor_space": "Outdoor space"}
# house-voice phrases for the "your taste in a sentence" readout (spec 11 delight)
_LIKE_PHRASE = {"design_finish": "a sharp, considered finish", "light_and_volume": "light and volume",
                "character_bones": "character and good bones", "outdoor_space": "proper outdoor space",
                "width_proportion_flow": "generous proportions", "raw_size_threshold": "real space"}
_AVOID_PHRASE = {"design_finish": "aren’t precious about the finish",
                 "light_and_volume": "can live without much light",
                 "character_bones": "don’t need period character",
                 "outdoor_space": "aren’t fussed about outdoor space",
                 "width_proportion_flow": "don’t mind tighter proportions",
                 "raw_size_threshold": "are fine with a smaller footprint"}


def _price_num(p: Any) -> Optional[int]:
    """A listing's price string ('£1,250,000' / '£4,000 pcm') -> a number, for tiering."""
    import re
    d = re.sub(r"[^0-9]", "", str(p or ""))
    return int(d) if d else None


def _load(p):
    with open(p) as f:
        return json.load(f)


_RENT_POOL = None


def _rent_pool():
    """The area asking-rent cohort for the rent verdict — loaded once."""
    global _RENT_POOL
    if _RENT_POOL is None:
        from gaff_engine.rent import load_rent_pool
        try:
            _RENT_POOL = load_rent_pool()
        except Exception:
            _RENT_POOL = []
    return _RENT_POOL


def _uniform():
    return {a: round(1.0 / len(AXES), 4) for a in AXES}


def learn_fit(history, axes, lam=0.05):
    """Signed directional preference model: ridge regression of reaction on the axis
    scores WITHOUT the non-negative constraint — so an axis you DISLIKE (you react low
    when it's high) gets a NEGATIVE coefficient and pulls a home's fit down. That's the
    'I actively don't want ornate period detail' signal that learn_weights' non-negative
    simplex throws away. Returns (coefs {axis: signed}, intercept); lam shrinks the axis
    coefficients (not the intercept) to tame overfit on ~15 points."""
    d = len(axes)
    if not history:
        return {a: 0.0 for a in axes}, 5.0
    rows = [[float(reads.get(a, 5.0)) / 10.0 for a in axes] + [1.0] for reads, _ in history]
    y = [float(t) / 10.0 for _, t in history]
    m, n = len(rows), d + 1
    A = [[0.0] * n for _ in range(n)]
    b = [0.0] * n
    for xi, yi in zip(rows, y):
        for i in range(n):
            b[i] += xi[i] * yi
            for j in range(n):
                A[i][j] += xi[i] * xi[j]
    for i in range(d):
        A[i][i] += lam * m                       # regularise the axis coefs, not the intercept
    coef = _solve(A, b)
    return {a: coef[i] for i, a in enumerate(axes)}, coef[d]


def _fit_score(coefs, intercept, reads):
    v = float(intercept) + sum(float(coefs.get(a, 0.0)) * float(reads.get(a, 5.0)) / 10.0 for a in coefs)
    return max(0.0, min(10.0, 10.0 * v))


def _vision_response(v: Dict[str, Any]) -> Dict[str, Any]:
    """A cohort vision read -> the taste-model response shape taste_result replays."""
    axes = {}
    for a in AXES:
        if a in v.get("axes", {}):
            axes[a] = {"score": float(v["axes"][a]["score"]),
                       "contribution": v["axes"][a].get("seen", "")}
        else:
            axes[a] = {"score": 5.0, "contribution": "not judged from photos"}
    return {"axes": axes, "namedLoveHits": [], "antiSignalHits": [], "staged": True}


def _is_rent_price(p: Any) -> bool:
    s = str(p or "").lower()
    return any(t in s for t in ("pcm", "pw", "per week", "per month", "per calendar"))


def _attach_rent_pcm(listing: Any, price: Any) -> None:
    """Give a buy-normalised rental its pcm so the rent verdict has a subject."""
    import re
    digits = re.sub(r"[^0-9]", "", str(price or ""))
    if not digits:
        return
    try:
        from gaff_engine.schemas import RentDetails, Money, MoneyPeriod
        listing.rent = RentDetails(rentPcm=Money(amount=int(digits), period=MoneyPeriod.PCM))
    except Exception:
        pass


def load_profiling_cohort(cohort_path: str = COHORT, vision_path: str = VISION):
    """Return the cohort with an OFFLINE model replaying the cached vision reads.
    Each home is tagged buy/rent and normalised on the matching path so its price +
    truth verdict make sense (a rental is not a purchase)."""
    manifest = _load(cohort_path)
    vision = _load(vision_path)
    homes = manifest["homes"]
    # dedupe: the same listing sometimes appears twice (relist / two agents) — collapse
    # by the first photo's FILENAME (Rightmove's image content-hash, identical for a
    # true duplicate even when the listing-id in the URL path differs).
    ids, _seen_photo = [], set()
    for i in manifest["ids"]:
        if i not in vision:
            continue
        ph = (homes[i].get("photos") or [None])[0]
        fn = ph.rsplit("/", 1)[-1] if ph else None
        if fn and fn in _seen_photo:
            continue
        if fn:
            _seen_photo.add(fn)
        ids.append(i)
    listings, reads, reads_by_id, prices, modes = {}, {}, {}, {}, {}
    for hid in ids:
        raw = _load(os.path.join(_ROOT, "data", "raw", "%s.json" % homes[hid]["raw_id"]))
        price = (raw.get("prices") or {}).get("primaryPrice")
        prices[hid] = price
        # propertyData path gives correct beds/outcode/sqft either way; for rentals we
        # attach the pcm so the rent verdict has a subject (rent_listing expects a
        # different, flat raw shape than these Rightmove payloads).
        listing = normalise(raw, today=TODAY)
        if _is_rent_price(price):
            modes[hid] = "rent"
            _attach_rent_pcm(listing, price)
        else:
            modes[hid] = "buy"
        listings[hid] = listing
        key = getattr(listing, "listingKey", None) or str(listing.id)
        resp = _vision_response(vision[hid])
        reads[key + "|text"] = resp
        reads[key + "|img"] = resp
        reads_by_id[hid] = {a: float(vision[hid]["axes"][a]["score"])
                            for a in AXES if a in vision[hid]["axes"]}
    model = LiveTasteModel(replay_model(reads))
    return {"homes": homes, "ids": ids, "listings": listings, "model": model,
            "reads_by_id": reads_by_id, "vision": vision, "prices": prices, "modes": modes}


def _salient_axis(reads: Dict[str, float], weights: Dict[str, float]) -> Optional[str]:
    """The axis this home most forms an opinion on: |read − neutral| × importance."""
    best, best_s = None, 0.0
    for a, sc in reads.items():
        s = float(weights.get(a, 0.0)) * abs(float(sc) - 5.0)
        if s > best_s:
            best_s, best = s, a
    return best


def _axis_dist(a: Dict[str, float], b: Dict[str, float]) -> float:
    keys = set(a) | set(b)
    return sum(abs(float(a.get(k, 5.0)) - float(b.get(k, 5.0))) for k in keys)


def _axis_spread(r: Dict[str, float]) -> float:
    vals = [float(v) for v in r.values()]
    return max(vals) - min(vals) if vals else 0.0


def _wdist(a: Dict[str, float], b: Dict[str, float], weights: Dict[str, float]) -> float:
    """Axis distance weighted so the axes you care about (per the intro/learned
    weights) count more — coverage leans toward what matters to you."""
    keys = set(a) | set(b)
    return sum((0.5 + float(weights.get(k, 0.0))) * abs(float(a.get(k, 5.0)) - float(b.get(k, 5.0)))
               for k in keys)


def select_calibration_home(reads_by_id, seen, weights=None, rng=None) -> Optional[str]:
    """Farthest-point coverage, weighted by the person's current axis weights (so
    what the intro said you care about gets probed harder) with a little randomness
    (so no two sessions show the identical set). Coverage still guarantees the
    love-AND-hate spread that weight-learning needs — pure EIG picked one-sided,
    all-lovable homes; a deterministic max picked the identical path every run."""
    weights = weights or {}
    rng = rng or random
    unseen = [(h, r) for h, r in reads_by_id.items() if h not in seen]
    if not unseen:
        return None
    if not seen:
        ranked = sorted(unseen, key=lambda hr: -_axis_spread(hr[1]))   # most opinion-forming
        return rng.choice(ranked[:8])[0]                                # random among the top = variety
    seen_vecs = [reads_by_id[h] for h in seen if h in reads_by_id]
    scored = sorted(unseen, key=lambda hr: -min(_wdist(hr[1], sv, weights) for sv in seen_vecs))
    return rng.choice(scored[:max(1, min(4, len(scored)))])[0]           # random among the farthest few


def contrastive_question(next_id, axis, history, reads_by_id, vision) -> Optional[Dict[str, Any]]:
    """Decision 2: name the variable the next home tests, framed against the prior
    reacted home that most contrasts it on ``axis``. None if no clean contrast."""
    if not axis or axis not in reads_by_id.get(next_id, {}):
        return None
    n_val = reads_by_id[next_id][axis]
    best_p, best_gap = None, 0.0
    for h in history:
        p = reads_by_id.get(h["id"], {})
        if axis not in p:
            continue
        gap = abs(p[axis] - n_val)
        opposite = (p[axis] - 5.0) * (n_val - 5.0) <= 0
        if gap >= 3.0 and (opposite or gap >= 4.0) and gap > best_gap:
            best_gap, best_p = gap, h
    if not best_p:
        return None
    react = {"love": "loved", "like": "liked", "meh": "were lukewarm on",
             "dislike": "passed on"}.get(best_p["reaction"], "saw")
    seen = (vision.get(next_id, {}).get("axes", {}).get(axis, {}).get("seen", "") or "")
    n_phrase = seen if len(seen) <= 82 else seen[:82].rsplit(" ", 1)[0] + "…"   # trim on a word
    label = _AXIS_LABEL.get(axis, axis)
    text = ("You %s the last place for %s. This one is different there — %s. On %s: more, or less?"
            % (react, label, n_phrase.rstrip("."), label))
    return {"text": text, "axis": axis, "contrastId": best_p["id"]}


class ProfilerSession:
    """One rater's calibration: cold-start (neutral weights) → ~15 selected reactions
    that LEARN the weights → held-out blind prediction. Held in memory."""

    def __init__(self, cohort: Dict[str, Any], name: str = "You", *,
                 priorities: Optional[List[str]] = None, loves: Optional[List[str]] = None,
                 anti: Optional[List[str]] = None, search_text: str = "",
                 holdout_n: int = 12, stop_reactions: int = 15,
                 elicitation: str = "rating", seed: Optional[int] = None):
        self.c = cohort
        self.stop_reactions = stop_reactions
        self.elicitation = elicitation
        self.stop_pairs = stop_reactions           # reuse the budget knob for duels
        self._rng = random.Random(seed)   # per-session: seed=None -> different homes each run
        self.holdout = _pick_holdout(cohort, holdout_n)
        self.calib_ids = [i for i in cohort["ids"] if i not in set(self.holdout)]
        self.calib_reads = {i: cohort["reads_by_id"][i] for i in self.calib_ids}
        self.person = person_from_answers({"name": name, "tastePriorities": list(priorities or []),
                                           "lovesNamed": list(loves or []), "household": "sharers",
                                           "minBeds": 2, "outdoorRequired": True})
        # PRIOR from the intro interview (else uniform); location axes start at 0 so
        # calibration stays location-blind — swipes then refine toward the aesthetics.
        w = dict(getattr(self.person.taste, "weights", None) or {}) if priorities else _uniform()
        for a in _LOCATION:
            w[a] = 0.0
        tot = sum(w.get(a, 0.0) for a in AXES) or 1.0
        self.prior = {a: round(w.get(a, 0.0) / tot, 4) for a in AXES}
        self.person.taste.weights = dict(self.prior)
        for sig in (anti or []):
            _write_anti_signal(self.person, sig, -6.0, False)
        self.uncertainty = seed_uncertainty(self.person, broadest_london_twin())
        self.history: List[Dict[str, Any]] = []
        self.seen: set = set()
        self.recent_axes: List[List[str]] = []
        self.blind: Dict[str, float] = {}
        self.name = name
        self.searchText = search_text
        self.fit_coefs: Dict[str, float] = {}    # signed directional model (learn_fit)
        self.fit_intercept: float = 5.0
        self._value_cache: Dict[str, Any] = {}
        self.seeded = {"priorities": list(priorities or []), "loves": list(loves or []),
                       "dislikes": list(anti or []), "parsed": bool(priorities or loves or anti)}
        if elicitation == "pairwise":
            self._pref_setup(priorities, anti)

    # -- calibration -------------------------------------------------------
    def next_home(self) -> Optional[Dict[str, Any]]:
        if len(self.history) >= self.stop_reactions:
            return None
        hid = select_calibration_home(self.calib_reads, self.seen,
                                      self.person.taste.weights, self._rng)
        if hid is None:
            return None
        cr = {a: v for a, v in self.calib_reads[hid].items() if a not in _LOCATION}
        axis = _salient_axis(cr, self.person.taste.weights)
        q = contrastive_question(hid, axis, self.history, self.c["reads_by_id"], self.c["vision"])
        return {"id": hid, "display": self.c["homes"].get(hid, {}), "axis": axis, "question": q}

    def react(self, hid: str, reaction: str, answer: Optional[str] = None) -> Dict[str, Any]:
        listing = self.c["listings"][hid]
        fb = swipe_feedback(listing, self.person, self.c["model"], _GESTURE.get(reaction, "left"))
        self.person, self.uncertainty, receipt, _ = apply_feedback(self.person, fb, self.uncertainty)
        reads = self.c["reads_by_id"][hid]
        calib_reads = {a: reads[a] for a in reads if a not in _LOCATION}   # location-blind
        target = _TARGET.get(reaction, 4.5)
        self.history.append({"id": hid, "reaction": reaction, "reads": calib_reads,
                             "target": target, "answer": (answer or None)})
        # THE FIX (spec 10 §2): re-learn the weights from every reaction so far.
        hist = [(h["reads"], h["target"]) for h in self.history]
        w = learn_weights(hist, self.prior, n=len(hist))              # non-neg importance (panel + selection)
        for a in _LOCATION:
            w[a] = 0.0                                                     # never learned here
        tot = sum(w.get(a, 0.0) for a in AXES) or 1.0
        self.person.taste.weights = {a: round(w.get(a, 0.0) / tot, 4) for a in AXES}
        self.fit_coefs, self.fit_intercept = learn_fit(hist, _CALIB)   # SIGNED fit (ranking + score)
        self.seen.add(hid)
        sa = _salient_axis(reads, self.person.taste.weights)
        self.recent_axes.append([sa] if sa else [])
        return {"reactions": len(self.history), "clarity": self._clarity(),
                "receipt": getattr(receipt, "line", None) or "",
                "topAxes": self._top_axes(3), "done": self.calibrated()}

    def _clarity(self) -> float:
        ov = getattr(self.uncertainty, "overall", None)
        return round(float(getattr(ov, "clarity0to1", 0.0) or 0.0), 3)

    def _top_axes(self, k: int) -> List[List[Any]]:
        w = self.person.taste.weights
        return [[a, round(w[a], 3)] for a in sorted(AXES, key=lambda a: -w[a])[:k]]

    def calibrated(self) -> bool:
        # 15 reactions is the anchor (Finn's call). No clarity early-out — clarity
        # measures belief-variance collapse, not accuracy, and fired misleadingly early.
        return len(self.history) >= self.stop_reactions

    def read(self) -> Dict[str, Any]:
        return {"weights": {a: round(self.person.taste.weights[a], 3) for a in AXES},
                "topAxes": self._top_axes(4), "clarity": self._clarity(),
                "namedLoves": list(getattr(self.person.taste, "lovesNamed", None) or []),
                "reactions": len(self.history)}

    # -- held-out (blind) --------------------------------------------------
    def holdout_homes(self) -> List[Dict[str, Any]]:
        return [{"id": i, "display": self.c["homes"].get(i, {})} for i in self.holdout]

    def record_blind(self, hid: str, rating: float) -> None:
        self.blind[str(hid)] = max(0.0, min(10.0, float(rating)))

    def predict(self) -> Dict[str, Any]:
        """Score each blind-rated held-out home with the real engine + learned Person."""
        rows, predicted, actual = [], {}, {}
        for hid in self.holdout:
            if hid not in self.blind:
                continue
            eng = round(self._score_home(hid), 1)
            you = self.blind[hid]
            predicted[hid], actual[hid] = eng, you
            rows.append({"id": hid, "engine": eng, "you": you, "err": round(abs(eng - you), 1)})
        out = {"rows": rows, "n": len(rows)}
        if rows:
            rep = evaluate(predicted, actual, label="profiler")
            out["mae"] = rep.mae
            if len(rows) >= 2:
                out["spearman"] = rep.spearman
        return out

    # -- pairwise elicitation (spec 11) — opt-in; the rating path above is untouched --
    def _pref_setup(self, priorities, anti) -> None:
        self.pref_phi = {i: pref.feature_map(self.c["reads_by_id"][i],
                                             bool(self.c["vision"].get(i, {}).get("period_authentic")))
                         for i in self.c["ids"]}
        self.pref_mu, self.pref_sig = pref.intro_to_prior(
            {"tastePriorities": list(priorities or []), "antiSignals": list(anti or [])})
        self.pref_pricenum = {i: _price_num(self.c["prices"].get(i)) for i in self.c["ids"]}
        self.pref_obs: List[Tuple[List[float], int]] = []
        self.pref_seen: set = set()
        self.pref_rounds = 0
        self.pref_log: List[Dict[str, Any]] = []       # per-duel record, for undo
        self._redo: Optional[Tuple[str, str]] = None    # a pair to re-serve after an undo
        self._refit_pref()

    def _comparable(self, a: str, b: str) -> bool:
        """A fair duel: similar SIZE and price TIER, so the choice is about style not scale."""
        ha, hb = self.c["homes"].get(a, {}), self.c["homes"].get(b, {})
        ba, bb = ha.get("beds"), hb.get("beds")
        if ba is not None and bb is not None and abs(ba - bb) > 1:
            return False
        pa, pb = self.pref_pricenum.get(a), self.pref_pricenum.get(b)
        if pa and pb and max(pa, pb) / max(1, min(pa, pb)) > 3.0:   # >3x apart = different market
            return False
        return True

    def _refit_pref(self) -> None:
        self.pref_w, self.pref_cov, *_ = pref.fit_posterior(self.pref_obs, self.pref_mu, self.pref_sig)
        pool = [pref.utility(self.pref_w, self.pref_phi[i]) for i in self.calib_ids]
        self.pref_ubar, self.pref_s = pref.calibrate_squash(pool)

    def next_pair(self) -> Optional[Dict[str, Any]]:
        if self.pref_rounds >= self.stop_pairs:
            return None
        if self._redo:                                  # re-serve the pair just undone
            a, b = self._redo
            self._redo = None
        else:
            q = pref.select_pair(self.calib_ids, self.pref_phi, self.pref_w, self.pref_cov,
                                 seen=self.pref_seen, comparable=self._comparable)
            if not q:
                return None
            a, b = q["aId"], q["bId"]
        z = [self.pref_phi[a][k] - self.pref_phi[b][k] for k in range(pref.DIM)]
        focus = pref.FEATURES[max(range(pref.DIM), key=lambda k: abs(z[k]))]
        label = ("how period vs modern it feels" if focus == pref.INTERACTION
                 else _MATCH_LABEL.get(focus, focus.replace("_", " ")))
        return {"aId": a, "bId": b, "a": self.c["homes"].get(a, {}), "b": self.c["homes"].get(b, {}),
                "axisFocus": focus, "axisLabel": label,
                "round": self.pref_rounds + 1, "of": self.stop_pairs}

    def react_pair(self, a_id: str, b_id: str, choice: str) -> Dict[str, Any]:
        """choice in {'a','b','skip'}. 'skip' spends the round but adds no observation."""
        self.pref_rounds += 1
        self.pref_seen.add(a_id)
        self.pref_seen.add(b_id)
        self.pref_log.append({"aId": a_id, "bId": b_id, "choice": choice})
        if choice in ("a", "b"):
            z = [self.pref_phi[a_id][k] - self.pref_phi[b_id][k] for k in range(pref.DIM)]
            self.pref_obs.append((z, 1 if choice == "a" else 0))
            self._refit_pref()
        conf = self.confidence()
        return {"pairs": self.pref_rounds, "confidence": conf["overall0to1"],
                "topAxes": conf["topAxes"], "leastResolved": conf["leastResolved"],
                "canUndo": bool(self.pref_log), "done": self.pairwise_calibrated()}

    def undo_pair(self) -> Dict[str, Any]:
        """Undo the last duel (for a mis-click) and re-serve that exact pair."""
        if not self.pref_log:
            return {"ok": False}
        e = self.pref_log.pop()
        self.pref_rounds = max(0, self.pref_rounds - 1)
        self.pref_seen.discard(e["aId"])
        self.pref_seen.discard(e["bId"])
        if e["choice"] in ("a", "b") and self.pref_obs:
            self.pref_obs.pop()
            self._refit_pref()
        self._redo = (e["aId"], e["bId"])
        conf = self.confidence()
        return {"ok": True, "pairs": self.pref_rounds, "confidence": conf["overall0to1"],
                "topAxes": conf["topAxes"], "leastResolved": conf["leastResolved"]}

    def pairwise_calibrated(self) -> bool:
        return self.pref_rounds >= self.stop_pairs

    def taste_sentence(self) -> str:
        """Your taste in one honest, house-voice line, straight from the posterior."""
        w = self.pref_w
        val = lambda a: w[pref.FEATURES.index(a)]
        pos = sorted([a for a in _CALIB if val(a) > 0.08], key=lambda a: -val(a))
        neg = sorted([a for a in _CALIB if val(a) < -0.08], key=lambda a: val(a))
        parts = []
        likes = [_LIKE_PHRASE[a] for a in pos[:2] if a in _LIKE_PHRASE]
        if likes:
            if len(likes) == 1:
                joined = likes[0]
            elif " and " in likes[0] or " and " in likes[1]:
                joined = likes[0] + ", " + likes[1]          # avoid an awkward triple 'and'
            else:
                joined = likes[0] + " and " + likes[1]
            parts.append("go for " + joined)
        wi = val(pref.INTERACTION) if pref.INTERACTION in pref.FEATURES else 0.0
        if wi < -0.15:
            parts.append("lean modern over period")
        elif wi > 0.15:
            parts.append("love genuine period soul")
        av = [_AVOID_PHRASE[a] for a in neg[:1] if a in _AVOID_PHRASE]
        if av:
            parts.append(av[0])
        if not parts:
            return "Still forming a picture of your taste — a few more choices will sharpen it."
        body = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + ", and " + parts[-1]
        return "You " + body + "."

    def confidence(self) -> Dict[str, Any]:
        ac = pref.axis_confidence(self.pref_cov, self.pref_sig, self.pref_w)
        cand = [i for i in sorted(range(pref.DIM), key=lambda i: -abs(self.pref_w[i]))
                if pref.FEATURES[i] != pref.INTERACTION]
        picks = cand[:3]
        pos = [i for i in cand if self.pref_w[i] > 0.05]      # surface a 'likes' if they have one
        if pos and not any(self.pref_w[i] > 0.05 for i in picks):
            picks = picks[:2] + [pos[0]]
        top = [{"axis": pref.FEATURES[i], "label": _MATCH_LABEL.get(pref.FEATURES[i], pref.FEATURES[i].replace("_", " ")),
                "dir": "up" if self.pref_w[i] >= 0 else "down",
                "resolved": round(ac["perAxis"][pref.FEATURES[i]]["resolved0to1"], 2)} for i in picks]
        least = [{"axis": a, "label": _MATCH_LABEL.get(a, a.replace("_", " "))}
                 for a in ac["leastResolved"] if a != pref.INTERACTION][:2]
        return {"overall0to1": round(ac["overall0to1"], 3), "topAxes": top,
                "leastResolved": least, "sentence": self.taste_sentence()}

    def _shown(self) -> set:
        """Homes already shown to the rater (rating reactions + pairwise duels)."""
        return self.seen | getattr(self, "pref_seen", set())

    # -- scoring dispatch: pairwise uses the posterior, rating uses the signed ridge --
    def _score_home(self, hid: str) -> float:
        if self.elicitation == "pairwise":
            sc, _ = pref.home_score(self.pref_w, self.pref_cov, self.pref_phi[hid],
                                    self.pref_ubar, self.pref_s)
            return sc
        return _fit_score(self.fit_coefs, self.fit_intercept, self.c["reads_by_id"][hid])

    def _score_band(self, hid: str) -> Optional[float]:
        if self.elicitation != "pairwise":
            return None
        _, band = pref.home_score(self.pref_w, self.pref_cov, self.pref_phi[hid],
                                  self.pref_ubar, self.pref_s)
        return round(band, 1)

    def _effective_coef(self, axis: str, hid: str) -> float:
        """Signed coefficient of an axis FOR THIS HOME — in pairwise mode the period
        interaction folds into character_bones when the home is period-authentic."""
        if self.elicitation != "pairwise":
            return float(self.fit_coefs.get(axis, 0.0))
        base = self.pref_w[pref.FEATURES.index(axis)] if axis in pref.FEATURES else 0.0
        if axis == "character_bones" and bool(self.c["vision"].get(hid, {}).get("period_authentic")):
            base += self.pref_w[pref.FEATURES.index(pref.INTERACTION)]
        return base

    # -- phase 3: apply the learned taste to a search (spec 10 §4) ----------
    def available_areas(self, mode: str = "buy") -> List[Dict[str, Any]]:
        """The areas we can show matches in, for buy or rent — cohort areas among
        homes the rater hasn't already seen (calibration + held-out excluded)."""
        from collections import Counter
        used = self._shown() | set(self.holdout)
        c = Counter(self.c["homes"][i].get("area", "?") for i in self.c["ids"]
                    if i not in used and self.c["modes"].get(i) == mode)
        return [{"area": a, "n": n} for a, n in c.most_common() if a and a != "?"]

    def shortlist(self, area: Optional[str] = None, min_beds: Optional[int] = None,
                  max_price: Optional[float] = None, mode: str = "buy",
                  k: int = 8) -> Dict[str, Any]:
        """Score fresh homes with the LEARNED taste and rank them — 'now I know your
        taste, here's what matches you'. Pool = same-mode cohort homes the rater
        didn't calibrate on, optionally filtered by area / beds."""
        used = self._shown() | set(self.holdout)
        rows = []
        for hid in self.c["ids"]:
            if hid in used or self.c["modes"].get(hid) != mode:
                continue
            h = self.c["homes"][hid]
            if area and h.get("area") != area:
                continue
            if min_beds and (h.get("beds") or 0) < min_beds:
                continue
            score = round(self._score_home(hid), 1)
            v = self.c["vision"].get(hid, {})
            rows.append({"id": hid, "display": h, "taste": score, "band": self._score_band(hid),
                         "price": self.c["prices"].get(hid),
                         "mode": mode, "why": self._why(hid),
                         "vibe": v.get("finish_style", ""),
                         "flip": bool(v.get("cheap_flip")), "authentic": bool(v.get("period_authentic"))})
        rows.sort(key=lambda r: -r["taste"])
        return {"area": area, "mode": mode, "rows": rows[:k], "n": len(rows)}

    def _why(self, hid: str) -> List[str]:
        """The two axes that most LIFT this home's fit for you: highest positive
        (signed coef x the home's score). Works for either elicitation path."""
        reads = self.c["reads_by_id"][hid]
        scored = [(a, self._effective_coef(a, hid) * float(reads.get(a, 5.0)))
                  for a in reads if a not in _LOCATION]
        scored = sorted([x for x in scored if x[1] > 0.3], key=lambda x: -x[1])
        return [_MATCH_LABEL.get(a, a) for a, _ in scored[:2]]

    def home_detail(self, hid: str) -> Optional[Dict[str, Any]]:
        """The per-home dashboard: WHY this scores what it does for you — each axis's
        score and whether your learned preference LIFTS it or PULLS IT DOWN (signed),
        with the vision evidence, biggest movers first."""
        if hid not in self.c["listings"]:
            return None
        reads = self.c["reads_by_id"][hid]
        v = self.c["vision"].get(hid, {})
        rows = []
        for a in _CALIB:
            if a not in reads:
                continue
            coef = self._effective_coef(a, hid)
            rows.append({"axis": a, "label": _DETAIL_LABEL.get(a, a),
                         "score": round(float(reads[a]), 1), "weight": round(coef, 2),  # SIGNED
                         "contribution": v.get("axes", {}).get(a, {}).get("seen", "")})
        rows.sort(key=lambda r: -abs(r["weight"] * r["score"]))   # biggest movers (up or down) first
        return {"id": hid, "display": self.c["homes"][hid],
                "taste": round(self._score_home(hid), 1), "band": self._score_band(hid),
                "breakdown": rows, "price": self.c["prices"].get(hid),
                "vision": {"one_line": v.get("one_line", ""), "finish_style": v.get("finish_style", ""),
                           "period_authentic": bool(v.get("period_authentic")),
                           "cheap_flip": bool(v.get("cheap_flip"))}}

    def value_for(self, hid: str) -> Dict[str, Any]:
        """The truth layer for one home: is it fairly priced? Buy -> vs real sold
        comps (Land Registry); rent -> vs the area's asking-rent cohort. Live,
        fail-soft, cached per home."""
        if hid not in self.c["listings"]:
            return {"state": "error"}
        if hid in self._value_cache:
            return self._value_cache[hid]
        out = (self._rent_value(hid) if self.c["modes"].get(hid) == "rent"
               else self._buy_value(hid))
        # Only pin verdicts the system actually knows. needs_data and error can
        # both be produced by a transient fetch failure (fetch_street degrades
        # to [] with a warning), and pinning one would assert "no comparable
        # sales nearby" for the rest of the session on the strength of an
        # outage. Uncached, the next call retries; disk caches make that cheap.
        if out.get("state") == "ok":
            self._value_cache[hid] = out
        return out

    def _buy_value(self, hid: str) -> Dict[str, Any]:
        try:
            from gaff_engine import enrich
            from gaff_engine.value import value_verdict
            listing = self.c["listings"][hid]
            vv = value_verdict(listing, enrich.enrich_for_listing(listing))
            tag = getattr(getattr(vv, "tag", None), "value", getattr(vv, "tag", None))
            asking = None
            try:
                asking = listing.buy.price.amount
            except Exception:
                pass
            if tag == "needs_data":
                return {"state": "needs_data", "mode": "buy", "basis": getattr(vv, "basis", "")}
            return {"state": "ok", "mode": "buy", "tag": tag, "asking": asking, "unit": "",
                    "fairEstimate": getattr(vv, "fairEstimate", None),
                    "deltaPct": getattr(vv, "deltaPct", None),
                    "confidence": getattr(vv, "confidence", None), "basis": getattr(vv, "basis", "")}
        except Exception:
            return {"state": "error", "mode": "buy"}

    def _rent_value(self, hid: str) -> Dict[str, Any]:
        try:
            import re
            from gaff_engine.rent import rent_verdict
            vv = rent_verdict(self.c["listings"][hid], _rent_pool())
            tag = getattr(getattr(vv, "tag", None), "value", getattr(vv, "tag", None))
            digits = re.sub(r"[^0-9]", "", str(self.c["prices"].get(hid) or ""))
            asking = int(digits) if digits else None
            if tag == "needs_data":
                return {"state": "needs_data", "mode": "rent", "basis": getattr(vv, "basis", "")}
            return {"state": "ok", "mode": "rent", "tag": tag, "asking": asking, "unit": " pcm",
                    "fairEstimate": getattr(vv, "fairEstimate", None),
                    "deltaPct": getattr(vv, "deltaPct", None),
                    "confidence": getattr(vv, "confidence", None), "basis": getattr(vv, "basis", "")}
        except Exception:
            return {"state": "error", "mode": "rent"}

    def log(self, prediction: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"name": self.name, "reactions": self.history,
                "learnedWeights": {a: round(self.person.taste.weights[a], 3) for a in AXES},
                "namedLoves": list(getattr(self.person.taste, "lovesNamed", None) or []),
                "blind": self.blind, "finalClarity": self._clarity(), "prediction": prediction}


def _pick_holdout(cohort: Dict[str, Any], n: int) -> List[str]:
    """A diverse, DETERMINISTIC held-out set (same across runs, so different personas
    are tested on one common yardstick): deterministic farthest-point over the vision
    axis vectors, so the 12 span the whole taste space and personas who weight
    different axes will rank them differently — a discriminating test."""
    reads = cohort["reads_by_id"]
    ids = sorted(cohort["ids"])
    if len(ids) <= n:
        return ids
    picked = [max(ids, key=lambda i: reads[i].get("design_finish", 5.0))]  # a fixed anchor
    while len(picked) < n:
        picked.append(max((i for i in ids if i not in picked),
                          key=lambda i: min(_axis_dist(reads[i], reads[p]) for p in picked)))
    return picked


__all__ = ["ProfilerSession", "load_profiling_cohort", "select_calibration_home",
           "contrastive_question", "COHORT", "VISION"]
