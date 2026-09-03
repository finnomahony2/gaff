"""P8 · Multiplayer — invite, fit-for-both, veto and voting (08-action.md §5.5).

the founding mission was literally "rent with a housemate" — the real product is
rarely single-player. Multiplayer adds a second (or third) person to a Search so a
home is scored for **both**, and resolves the coordination (veto + shortlist voting)
**without a second engine or a mutated Person**. It reuses Person portability
(P1 E / P5 A14): each member has their own portable Person; the Listing is scored
once per Person against the *same* Search config; the results combine here.

Three pieces:
- **`combine_fit`** (§5.5b) — merges the members' `score.result@1`s into one shared
  verdict by `harmonic_floor`: `harmonicMean(composites) × agreementFactor`, with a
  veto forcing the score to 0. Harmonic mean punishes a low outlier harder than an
  average (8.5/3.0 → 4.4, not 5.75), so a home one person hates cannot ride the
  other's love into the shared shortlist; `agreementFactor` further damps genuinely
  split homes.
- **`make_invite` / `accept_invite`** (§5.5a) — the invite lifecycle over P1's
  `search.collaborators` and the role enum; on accept the invitee's `personRef` is
  written into the collaborators.
- **`make_vote` / `shared_shortlist`** (§5.5c) — a `vote@1` (a veto REQUIRES a
  reason) and the shared-shortlist ordering (by combined score, vetoed homes gated
  out).

Pure and deterministic — a function of the members' scores. It writes no Person and
runs no engine (the scores are produced upstream, once per portable Person).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from gaff_engine.schemas import CollabInvite, Collaborator, CombinedFit, MemberFit, Ref, Vote


def _g(obj: Any, name: str, default: Any = None) -> Any:
    cur = obj
    for part in name.split("."):
        if cur is None:
            return default
        cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
    return default if cur is None else cur


def _enum(v: Any) -> Any:
    return getattr(v, "value", v)


def harmonic_mean(values: List[float]) -> float:
    """Harmonic mean — punishes a low outlier harder than the arithmetic mean
    (the both-must-love property). Undefined at 0, so a 0 collapses it to 0."""
    vals = [float(v) for v in values]
    if not vals or any(v <= 0 for v in vals):
        return 0.0
    return len(vals) / sum(1.0 / v for v in vals)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _top_reason(score: Any) -> Optional[str]:
    reasons = _g(score, "reasons") or []
    for r in reasons:
        if _g(r, "polarity") == "+":
            return _g(r, "text")
    return _g(reasons[0], "text") if reasons else None


def _top_flag(score: Any) -> Optional[str]:
    sev = {"serious": 0, "watch": 1, "info": 2}
    flags = _g(score, "flags") or []
    if not flags:
        return None
    f = sorted(flags, key=lambda x: sev.get(_enum(_g(x, "severity")), 9))[0]
    return "%s (%s)" % (_enum(_g(f, "code")), _enum(_g(f, "severity")))


# ---------------------------------------------------------------------------
# §5.5b — combine_fit (harmonic_floor, veto-aware).
# ---------------------------------------------------------------------------

def combine_fit(members: List[Dict[str, Any]], *, search_ref: Ref, listing_ref: Ref,
                fit_id: str, method: str = "harmonic_floor",
                generated_at: str = "2026-07-14T10:00:00Z") -> CombinedFit:
    """Merge N members' reads into one `fit.combined@1` (§5.5b). Each member dict is
    ``{person_ref, score, score_ref, veto?, veto_reason?}`` where ``score`` is that
    member's own ``score.result@1``. A veto forces the combined score to 0 and gates
    the home out of the shared shortlist (§5.5c)."""
    fits: List[MemberFit] = []
    composites: List[float] = []
    veto_by: List[Ref] = []
    for m in members:
        comp = float(_g(m["score"], "composite") or 0.0)
        veto = bool(m.get("veto"))
        composites.append(comp)
        if veto:
            veto_by.append(m["person_ref"])
        fits.append(MemberFit(
            personRef=m["person_ref"], scoreResultRef=m["score_ref"], composite=comp, veto=veto,
            topReasonForThem=_top_reason(m["score"]), topFlagForThem=_top_flag(m["score"])))

    vetoed = len(veto_by) > 0
    if vetoed:
        score = 0.0
        agreement = round(1.0 - (max(composites) - min(composites)) / 10.0, 2) if composites else 1.0
    else:
        hm = harmonic_mean(composites)
        spread = (max(composites) - min(composites)) if composites else 0.0
        agreement = round(1.0 - spread / 10.0, 2)
        factor = _clamp(0.6 + 0.4 * agreement, 0.6, 1.0)
        score = round(hm * factor, 1)

    top = max(composites) if composites else 0.0
    dissent = [{"personRef": f.personRef, "theirScore": f.composite, "gap": round(top - f.composite, 1)}
               for f in fits if (top - f.composite) > 0.1]

    return CombinedFit(
        id=fit_id, searchRef=search_ref, listingRef=listing_ref, memberFits=fits,
        combined={"score": score, "method": method, "agreement": agreement, "dissent": dissent},
        vetoed=vetoed, generatedAt=generated_at, vetoBy=veto_by or None)


# ---------------------------------------------------------------------------
# §5.5a — the invite lifecycle.
# ---------------------------------------------------------------------------

def make_invite(search_ref: Ref, invited_email: str, invited_by: Ref, role: str, *,
                invite_id: str, message: Optional[str] = None,
                created_at: str = "2026-07-14T09:00:00Z") -> CollabInvite:
    """Create a `pending` collab.invite@1 (§5.5a). Uses ONLY the P1 role enum
    (editor|viewer); the actual send is a permissioned user action the platform gates."""
    if role not in ("editor", "viewer"):
        raise ValueError("collab invite role must be editor|viewer (owner is the inviter)")
    return CollabInvite(id=invite_id, searchRef=search_ref, invitedEmail=invited_email,
                        invitedByPersonRef=invited_by, role=role, status="pending",
                        message=message, createdAt=created_at)


def accept_invite(invite: CollabInvite, person_ref: Ref, *,
                  responded_at: str = "2026-07-14T09:30:00Z") -> (CollabInvite, Collaborator):
    """Accept an invite (§5.5a): status→accepted, the invitee's personRef recorded,
    and the Collaborator to append to `search.collaborators` (P1). The invitee brings
    their OWN portable Person — the point of the Person-vs-Search split."""
    import copy
    inv = copy.deepcopy(invite)
    inv.status = "accepted"
    inv.personRef = person_ref
    inv.respondedAt = responded_at
    from gaff_engine.schemas import Role
    collab = Collaborator(email=inv.invitedEmail, role=Role(inv.role), personRef=person_ref)
    return inv, collab


# ---------------------------------------------------------------------------
# §5.5c — votes + the shared shortlist.
# ---------------------------------------------------------------------------

def make_vote(pursuit_ref: Ref, search_ref: Ref, listing_ref: Ref, person_ref: Ref, value: str, *,
              vote_id: str, reason: Optional[str] = None,
              created_at: str = "2026-07-14T10:05:00Z") -> Vote:
    """Cast a vote@1 (§5.5c). `up`/`down` are shortlist votes; `veto` is a hard down
    that REQUIRES a reason (a veto with no reason is invalid) and is reversible."""
    if value not in ("up", "down", "veto"):
        raise ValueError("vote value must be up|down|veto")
    if value == "veto" and not (reason and reason.strip()):
        raise ValueError("a veto requires a reason — 'vetoes welcome' is paired with accountability")
    return Vote(id=vote_id, pursuitRef=pursuit_ref, searchRef=search_ref, listingRef=listing_ref,
                personRef=person_ref, value=value, reason=reason, createdAt=created_at)


def tally(votes: List[Vote]) -> int:
    """Net shortlist tally for a pursuit: +1 per up, −1 per down (veto handled by the fit)."""
    return sum(1 if v.value == "up" else -1 if v.value == "down" else 0 for v in votes)


def shared_shortlist(fits: List[CombinedFit], votes_by_listing: Optional[Dict[str, List[Vote]]] = None
                     ) -> List[CombinedFit]:
    """Order the shared shortlist (§5.5c): vetoed homes gated out, the rest ranked by
    combined score desc, ties broken by net vote tally."""
    votes_by_listing = votes_by_listing or {}
    live = [f for f in fits if not f.vetoed]
    return sorted(live, key=lambda f: (-(f.combined["score"] or 0.0),
                                       -tally(votes_by_listing.get(f.listingRef.id, []))))


__all__ = [
    "harmonic_mean", "combine_fit", "make_invite", "accept_invite", "make_vote", "tally",
    "shared_shortlist",
]
