"""U-alert — the P8 alerts delivery engine (08-action.md §5.4).

Where a browse tool becomes a standing service. Two pure functions over P1's
per-Search `alertPolicy` + `threshold`:

* **`evaluate_alert(score, listing, search, ...) → alert.event@1`** — decides
  *whether* a scored listing alerts. The **only-surface-7-plus gate** is the
  north-star noise-inversion made felt (00-frame "listings-shown per home-loved,
  lower is better"): a new score alerts only if it passed the hard Gates AND
  `composite ≥ minComposite` (default `threshold.alert` = 7.5) AND it isn't a
  dedupe. Everything below the gate stays visible in-app but never interrupts a
  life. The **protective** `verdict_change` alert (a shortlisted home re-scored
  `over`, or a new serious flag) is the exception: it bypasses the composite gate
  (saved-from-a-mistake outranks noise-suppression) but still respects quiet hours.
* **`assemble_digest(events, search, ...) → digest@1`** — batches the window's
  pending events into one delivery, ranked by composite desc (protective to top),
  capped at `maxPerDigest` with the overflow `rolledOver` (never dropped), held
  through quiet hours. The `narration` cites the items, never invents (P5 rule 5).

Pure + deterministic + stdlib-only. It reads a `score.result@1` and never
recomputes one; it writes only P8-owned objects (A13).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from gaff_engine.schemas import (
    AlertEvent, AlertGate, AlertState, AlertType, Digest, Ref,
)

# ---------------------------------------------------------------------------
# alertpolicy.config@1 (§5.0) — the system defaults/curve the gate reads; P1's
# per-Search alertPolicy is the per-user policy layered on top.
# ---------------------------------------------------------------------------

ALERT_CONFIG: Dict[str, Any] = {
    "schemaVersion": "alertpolicy.config@1",
    "version": "1.0.0",
    "digestWindows": {"daily": "07:30", "weekly": "Mon 07:30"},
    "instantDebounceMinutes": 15,
    "maxPerDigestDefault": 8,
    "dedupeKey": "listingKey+type",
    "protectiveBypassesCompositeGate": True,
    "protectiveRespectsQuietHours": True,
    "backOnMarketReAlertAfterDays": 30,
    "defaultAlertThreshold": 7.5,            # the Buy threshold.alert default (the "7-plus")
}

PROTECTIVE = AlertType.VERDICT_CHANGE


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute-or-key among names (dotted walks in)."""
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


def _wire(v: Any) -> Any:
    return getattr(v, "value", v)


def _min_composite(search: Any, config: Dict[str, Any]) -> float:
    """alertPolicy.minComposite, defaulting to threshold.alert, defaulting to 7.5."""
    m = _g(search, "alertPolicy.minComposite")
    if m is not None:
        return float(m)
    t = _g(search, "threshold.alert")
    return float(t) if t is not None else config["defaultAlertThreshold"]


# ---------------------------------------------------------------------------
# evaluate_alert — classify the trigger, apply the only-surface-7-plus gate.
# ---------------------------------------------------------------------------

def evaluate_alert(score_result: Any, listing: Any, search: Any, *,
                   prior: Optional[Dict[str, Any]] = None, seen_keys: Optional[set] = None,
                   now: Optional[str] = None, config: Dict[str, Any] = ALERT_CONFIG) -> AlertEvent:
    """Evaluate one newly-scored listing against a Search's alert policy → an
    `alert.event@1` (`state:pending` if it clears, else `suppressed` with an
    honest `suppressReason`). Pure; classifies the trigger by comparing the new
    score to the pursuit's `prior` state (None ⇒ a fresh listing).

    `prior` (the pursuit's last-known state, if saved): `{ saved, price, tag,
    seriousFlagCodes:set, status, soldOrWithdrawn, soldSince Days }`.
    """
    excluded = bool(_g(score_result, "rules.excluded", default=False))
    composite = float(_g(score_result, "composite", default=0.0) or 0.0)
    min_comp = _min_composite(search, config)
    channel = _g(search, "alertPolicy.channel", default="digest")
    key = _g(listing, "listingKey", "id")
    saved = bool(prior and prior.get("saved"))

    a_type, trigger = _classify(score_result, listing, prior, saved, config)

    dedupe_key = "%s+%s" % (key, _wire(a_type))
    protective = a_type == PROTECTIVE
    # Which triggers must re-clear the composite gate: a fresh discovery
    # (new_match / back_on_market). A saved listing's price/status/verdict change
    # is already past the gate; the protective one bypasses it outright.
    gate_applies = a_type in (AlertType.NEW_MATCH, AlertType.BACK_ON_MARKET)

    suppress = None
    if excluded:
        suppress = "excluded"                                  # composite 0 → never alerts
    elif gate_applies and composite < min_comp:
        suppress = "below_min_composite"
    elif seen_keys is not None and dedupe_key in seen_keys:
        suppress = "dedupe"

    passed = suppress is None
    if passed and seen_keys is not None:
        seen_keys.add(dedupe_key)

    gate = AlertGate(minComposite=round(min_comp, 2),
                     passed=passed and not excluded,
                     protectiveBypass=protective and passed)

    return AlertEvent(
        id="alert_%s_%s" % (_wire(a_type), key),
        searchRef=Ref(id=_g(search, "id"), schemaVersion="search@1"),
        listingRef=Ref(id=_g(listing, "id"), schemaVersion="listing@1"),
        scoreResultRef=Ref(id=_g(score_result, "id"), schemaVersion="score.result@1"),
        pursuitRef=(Ref(id=prior.get("pursuitId"), schemaVersion="pursuit@1")
                    if prior and prior.get("pursuitId") else None),
        type=a_type, trigger=trigger, composite=round(composite, 1), gate=gate,
        channel=channel,
        state=AlertState.PENDING if passed else AlertState.SUPPRESSED,
        suppressReason=suppress, createdAt=now, deliveredAt=None)


def _classify(score_result: Any, listing: Any, prior: Optional[Dict[str, Any]],
              saved: bool, config: Dict[str, Any]) -> Tuple[AlertType, Dict[str, Any]]:
    """Pick the trigger type + its {was, now, delta}. Protective (verdict_change)
    ranks first, then price_drop, back_on_market, status_change; else new_match."""
    tag = _wire(_g(score_result, "valueVerdict.tag"))
    serious_now = {_wire(_g(f, "code")) for f in (_g(score_result, "flags") or [])
                   if _wire(_g(f, "severity")) == "serious"}

    if saved and prior is not None:
        prior_tag = prior.get("tag")
        prior_serious = set(prior.get("seriousFlagCodes") or [])
        new_serious = serious_now - prior_serious
        # verdict_change (PROTECTIVE): flipped to `over`, or a new serious flag.
        if (tag == "over" and prior_tag != "over") or new_serious:
            was = {"tag": prior_tag} if (tag == "over" and prior_tag != "over") else {"seriousFlags": sorted(prior_serious)}
            now_v = {"tag": tag} if (tag == "over" and prior_tag != "over") else {"seriousFlags": sorted(serious_now)}
            return PROTECTIVE, {"was": was, "now": now_v, "delta": sorted(new_serious) or None}
        # price_drop
        price_now = _g(listing, "buy.price.amount")
        price_was = prior.get("price")
        if price_now is not None and price_was is not None and price_now < price_was:
            return AlertType.PRICE_DROP, {"was": {"price": price_was}, "now": {"price": price_now},
                                          "delta": price_now - price_was}
        # back_on_market
        if prior.get("soldOrWithdrawn") and (prior.get("soldSinceDays") or 0) >= config["backOnMarketReAlertAfterDays"]:
            return AlertType.BACK_ON_MARKET, {"was": {"status": prior.get("status")},
                                              "now": {"status": "for_sale"}, "delta": None}
        # status_change (lease/chain/EPC/other material field)
        changed = prior.get("changedFields")
        if changed:
            return AlertType.STATUS_CHANGE, {"was": prior.get("statusWas") or {},
                                             "now": prior.get("statusNow") or {}, "delta": list(changed)}
    # A fresh listing (or a saved one with no change) → new_match on the gate.
    return AlertType.NEW_MATCH, {"was": None, "now": {"composite": round(float(_g(score_result, "composite", default=0.0) or 0.0), 1)}, "delta": None}


# ---------------------------------------------------------------------------
# assemble_digest — batch, rank, cap, roll over, hold for quiet hours.
# ---------------------------------------------------------------------------

def assemble_digest(events: List[AlertEvent], search: Any, *, window: Dict[str, Any],
                    person_ref: Optional[Ref] = None, now: Optional[str] = None,
                    digest_id: str = "digest_sample", config: Dict[str, Any] = ALERT_CONFIG) -> Optional[Digest]:
    """Batch the window's pending events into one `digest@1` (or None if none).
    Ranked composite desc with protective alerts sorted to the top; capped at
    `alertPolicy.maxPerDigest` (default 8) with the overflow `rolledOver` (never
    dropped); the send time held past quiet hours. Cadence instant/daily/weekly."""
    pending = [e for e in events if _wire(e.state) == "pending"]
    if not pending:
        return None

    cadence = _g(search, "alertPolicy.cadence", default="daily")
    channel = _g(search, "alertPolicy.channel", default="digest")
    cap = int(_g(search, "alertPolicy.maxPerDigest") or config["maxPerDigestDefault"])
    quiet = _g(search, "alertPolicy.quietHours")

    # Rank: protective first, then composite desc, then a stable id tiebreak.
    ranked = sorted(pending, key=lambda e: (0 if _wire(e.type) == "verdict_change" else 1,
                                            -float(e.composite or 0.0), e.id or ""))
    kept, overflow = ranked[:cap], ranked[cap:]
    capped = bool(overflow)

    fire = window.get("to") or now
    sent_at = _held_past_quiet(fire, quiet)
    for e in kept:
        e.state = AlertState.SENT
        e.deliveredAt = sent_at
    for e in overflow:
        e.state = AlertState.PENDING            # rolled to the next digest, not dropped

    return Digest(
        id=digest_id,
        searchRef=Ref(id=_g(search, "id"), schemaVersion="search@1"),
        personRef=person_ref or Ref(id=_g(search, "personRef.id"), schemaVersion="person@1"),
        cadence=cadence, window=dict(window), channel=channel,
        items=[Ref(id=e.id, schemaVersion="alert.event@1") for e in kept],
        count=len(kept), capped=capped,
        rolledOver=[Ref(id=e.id, schemaVersion="alert.event@1") for e in overflow],
        narration=_narrate(kept, capped, len(overflow)), sentAt=sent_at)


# ---------------------------------------------------------------------------
# Quiet hours — hold, never drop (§5.4). String HH:MM compare (deterministic).
# ---------------------------------------------------------------------------

def _time_in_quiet(hhmm: str, quiet: Optional[str]) -> bool:
    """Is a HH:MM inside the quiet range (which may wrap midnight, "22:00-07:00")?"""
    if not quiet or "-" not in quiet:
        return False
    start, end = (s.strip() for s in quiet.split("-", 1))
    if start <= end:
        return start <= hhmm < end
    return hhmm >= start or hhmm < end          # wraps midnight


def _held_past_quiet(iso: Optional[str], quiet: Optional[str]) -> Optional[str]:
    """The delivery time: `iso` if outside quiet hours, else the quiet window's
    close (same day if early-morning, next day if late-evening). Nothing dropped."""
    if not iso or not quiet or "-" not in quiet:
        return iso
    hhmm = iso[11:16] if len(iso) >= 16 else ""
    if not _time_in_quiet(hhmm, quiet):
        return iso
    end = quiet.split("-", 1)[1].strip()
    try:
        base = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return iso
    start = quiet.split("-", 1)[0].strip()
    # Late-evening (>= start) rolls to the next day's close; early-morning stays today.
    day = base + timedelta(days=1) if hhmm >= start else base
    return "%sT%s:00Z" % (day.strftime("%Y-%m-%d"), end)


# ---------------------------------------------------------------------------
# Narration — one house-voice line that CITES the items, never invents (rule 5).
# ---------------------------------------------------------------------------

def _narrate(items: List[AlertEvent], capped: bool, overflow_n: int) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for e in items:
        counts[_wire(e.type)] = counts.get(_wire(e.type), 0) + 1
    parts: List[str] = []
    label = {"new_match": ("new match", "new matches"), "price_drop": ("price drop", "price drops"),
             "verdict_change": ("value warning", "value warnings"),
             "back_on_market": ("back on the market", "back on the market"),
             "status_change": ("status change", "status changes")}
    order = ["verdict_change", "price_drop", "new_match", "back_on_market", "status_change"]
    for t in order:
        n = counts.get(t, 0)
        if n:
            sing, plur = label[t]
            parts.append("%d %s" % (n, sing if n == 1 else plur))
    headline = _join(parts) + (" cleared your alert line." if parts else "")
    protective = [e for e in items if _wire(e.type) == "verdict_change"]
    if protective:
        subhead = "One is a value warning on a home you shortlisted — worth a look before you go further."
    elif capped:
        subhead = "Ranked by fit; %d more rolled to tomorrow's digest so this stays a shortlist, not a feed." % overflow_n
    else:
        subhead = "Only the handful above your line — everything else stays in-app, off your notifications."
    return {"headline": headline[0].upper() + headline[1:] if headline else "Your digest.",
            "subhead": subhead}


def _join(parts: List[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


__all__ = ["ALERT_CONFIG", "evaluate_alert", "assemble_digest"]
