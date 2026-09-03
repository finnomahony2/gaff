"""P8 · Viewing mode — the retention hinge (08-action.md §5.1).

The viewing stage is where the score's **dual output** pays off: `taste.score` got
the buyer to the door; the `kind:"viewing"` flags protect them once inside. Two
pieces:

* **`generate_checklist(score, listing, search)`** (§5.1a) — PURE, deterministic.
  Turns the score's flags + a couple of standing buy-questions + the one unverified
  axis + the standing playbook into a sourced checklist. Every line cites what the
  engine surfaced; no line asserts a viewing-only fact from a `kind:"listing"` flag
  (A1). Priority maps from severity (serious→must, watch→should, info→nice).

* **`debrief(record, extracted, person, uncertainty, score)`** (§5.1c) — the voice
  debrief does three jobs: (1) resolves each checklist item from the extraction,
  (2) records whether the verdict held (`verdictAgreement`) + `savedFromMistake`,
  (3) **re-weights the portable Person** by constructing a `feedback@1`
  (`kind:"viewing_note"`) and calling **P4 `apply_feedback`** — the sanctioned
  write path. It does NOT reimplement the Bayesian update (P4 owns it); it builds
  the event and shows the receipt P4 returns. This is the moment Gaff visibly
  learns from the real world: clarity ticks up, "I've updated your profile."

The **transcript → extracted** step is LLM extraction — the parked wire-up; the
build feeds a recorded `DebriefExtracted` (the §5.1c worked debrief), and the
re-weight it drives is real. Boundary (A11): this module writes the Person ONLY
through `apply_feedback`; the viewing record mutates no Person field itself.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from gaff_engine.schemas import (
    ChecklistItem, Debrief, DebriefExtracted, Ref, ViewingRecord, VerdictAgreement,
)
from gaff_engine.swipe import CONFIG, Feedback, Observation, apply_feedback

_AT = "2026-07-14T15:30:00Z"        # a fixed clock for deterministic builds/tests
_VOICE_TAU = CONFIG["probeNoise"]["voice_rate"]   # 0.8 — a viewing note is a high-trust observation

_SEV_PRIORITY = {"serious": "must", "watch": "should", "info": "nice"}
_PRIORITY_RANK = {"must": 0, "should": 1, "nice": 2}

# The canonical axis only the human can verify (the maps/street pass), P3 OQ 8.5.
_AXIS_VERIFY = "street_scene"

_VIEWING_PROMPTS = {
    "lower_ground_light": "Stand in the rear lower-ground rooms at midday — real daylight, or borrowed?",
    "hmo_tells": "Look for the HMO tells — a second kitchen, ensuite clusters, bedroom locks.",
    "damp_risk": "Check the skirtings and the worst-facing corner for damp — smell as much as look.",
}
_LISTING_PROMPTS = {
    "epc_below_c": "Note the glazing and insulation — a negotiation lever, not a blocker.",
    "short_lease": "Confirm the lease term and the freeholder's stance on extending.",
    "flood_risk": "Ask the agent about any past flooding and check the EA flood history.",
}


def _g(obj: Any, name: str, default: Any = None) -> Any:
    cur = obj
    for part in name.split("."):
        if cur is None:
            return default
        cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
    return default if cur is None else cur


def _enum(v: Any) -> Any:
    return getattr(v, "value", v)


def _flags(score: Any) -> List[Any]:
    return list(_g(score, "flags") or [])


# ---------------------------------------------------------------------------
# §5.1a — generate the checklist (pure, deterministic).
# ---------------------------------------------------------------------------

def generate_checklist(score: Any, listing: Any, search: Any) -> List[ChecklistItem]:
    """Assemble the checklist from four sources in priority order (§5.1a). Pure:
    a function of the score. Returned in source order (viewing flags first); the
    `priority` field drives the must-first tick order the display sorts on."""
    items: List[ChecklistItem] = []
    n = 0

    def add(**kw):
        nonlocal n
        n += 1
        kw.setdefault("mediaRefs", [])
        kw.setdefault("status", "pending")
        items.append(ChecklistItem(id="vi_%d" % n, **kw))

    serious_listing = []   # serious kind:"listing" flags fold into hidden_faults (A1), not a viewing fact

    # 1. Viewing flags — the spine (the things you'd ONLY find at a viewing).
    for f in _flags(score):
        if _enum(_g(f, "kind")) != "viewing":
            continue
        code = _enum(_g(f, "code"))
        add(source="viewing_flag", kind="observe", flagCode=code,
            prompt=_VIEWING_PROMPTS.get(code, _g(f, "text") or "Check this in person."),
            priority=_SEV_PRIORITY.get(_enum(_g(f, "severity")), "should"),
            evidenceRef="flag:%s" % code)

    # collect serious listing flags to fold into the hidden_faults ask
    for f in _flags(score):
        if _enum(_g(f, "kind")) == "listing" and _enum(_g(f, "severity")) == "serious":
            serious_listing.append(f)

    # 2. Standing buy-questions (forensics-driven confirmations, §5.2f).
    add(source="buy_question", question="layout_works", kind="measure",
        prompt="Measure the narrowest reception wall — confirm the floorplan flows and isn't skinny.",
        priority="should", evidenceRef="forensics:roomWidthsM")
    if serious_listing:
        f = serious_listing[0]
        code = _enum(_g(f, "code"))
        add(source="buy_question", question="hidden_faults", kind="ask_agent", flagCode=code,
            prompt=_LISTING_PROMPTS.get(code, _g(f, "text") or "Confirm the serious flag with the agent."),
            priority="must", evidenceRef="flag:%s" % code)
    else:
        add(source="buy_question", question="hidden_faults", kind="ask_agent",
            prompt="Ask what's been done and what hasn't — recent works, guarantees, anything hidden.",
            priority="should", evidenceRef="playbook:hidden_faults")

    # 3. Axis to verify in person (the one the score assumed but couldn't stand on).
    add(source="axis_verify", axis=_AXIS_VERIFY, kind="observe",
        prompt="Walk the street — is the scene as good as the score assumed? (street_scene was scored unverified.)",
        priority="should", evidenceRef="axis:%s" % _AXIS_VERIFY)

    # 4. Non-serious listing flags worth an in-person note (never a viewing-only fact, A1).
    for f in _flags(score):
        if _enum(_g(f, "kind")) != "listing" or _enum(_g(f, "severity")) == "serious":
            continue
        code = _enum(_g(f, "code"))
        add(source="listing_flag", kind="observe", flagCode=code,
            prompt=_LISTING_PROMPTS.get(code, _g(f, "text") or "Note this in person."),
            priority=_SEV_PRIORITY.get(_enum(_g(f, "severity")), "nice"),
            evidenceRef="flag:%s" % code)

    # 5. Standing viewing playbook (mode-level ask_agent items; carry no per-listing claim).
    add(source="playbook", kind="ask_agent", priority="should",
        prompt="Onward chain — is the seller in a chain, and how far along is it?")
    add(source="playbook", kind="ask_agent", priority="nice",
        prompt="Why are they selling, and how long has it been on the market?")
    add(source="playbook", kind="ask_agent", priority="nice",
        prompt="What's included — fixtures, fittings, white goods, parking?")
    return items


def checklist_sorted(items: List[ChecklistItem]) -> List[ChecklistItem]:
    """The tick order: must first, then should, then nice — stable within (§5.1b)."""
    return sorted(items, key=lambda it: _PRIORITY_RANK.get(it.priority, 9))


def prepare_viewing(score: Any, listing: Any, search: Any, *, pursuit_ref: Ref,
                    person_ref: Ref, score_ref: Ref, viewing_id: str,
                    scheduled_for: Optional[str] = None) -> ViewingRecord:
    """Assemble a `prepared` viewing.record@1 with the generated checklist (§5.1)."""
    return ViewingRecord(
        id=viewing_id, pursuitRef=pursuit_ref,
        searchRef=Ref(id=_g(search, "id"), schemaVersion="search@1"),
        listingRef=Ref(id=_g(listing, "id"), schemaVersion="listing@1"),
        personRef=person_ref, scoreResultRef=score_ref,
        checklist=generate_checklist(score, listing, search),
        status="prepared", scheduledFor=scheduled_for,
        createdAt=_AT, updatedAt=_AT)


# ---------------------------------------------------------------------------
# Agent questions — uncertainty turned into action (outside-review brief §5.2).
#
# The checklist above turns the score's FLAGS into in-person checks; this turns
# the engine's own UNCERTAINTY — the gaps the verdict already recorded but a
# reader would have to know to look for — into per-listing "ask the agent"
# questions. Same design contract as generate_checklist (deterministic, every
# line sourced, no line asserts a fact the engine did not surface); it extends
# that machinery to a second output rather than forking it, and score_listing
# composes both. The review's framing: this is the moment the system becomes
# an analyst rather than a classifier.
# ---------------------------------------------------------------------------

# Claimed-works phrases that make a stale EPC area worth challenging. Matched
# deterministically over description + keyFeatures (no LLM), the same
# _text_blob idiom taste.py uses for its fallbacks.
_WORKS_PHRASES = ("refurbish", "renovat", "converted", "conversion",
                  "extension", "extended", "remodel", "reconfigur")

# What to check in person, per taste axis, when its score carries real weight
# but the read's evidence was thin. Mirrors _VIEWING_PROMPTS' voice.
_AXIS_IN_PERSON = {
    "light_and_volume": "stand in the main rooms at the darkest time you can view — real daylight, aspect, ceiling heights",
    "outdoor_space": "walk the outdoor space — size, orientation, overlooking",
    "character_bones": "check the period fabric up close — original features real or reproduction, proportions intact",
    "width_proportion_flow": "measure the narrowest reception wall and walk the circulation — does the plan actually flow",
    "street_scene": "walk the street both directions and at a second time of day",
    "raw_size_threshold": "pace the rooms against the floorplan — does the stated area feel honest",
    "design_finish": "look past the staging — finish quality at skirting/door/window level",
    "station_proximity": "walk the station route once, timed, not the crow-flies figure",
}


def _listing_blob(listing: Any) -> str:
    bits = []
    desc = _g(listing, "description")
    if desc:
        bits.append(str(desc))
    for k in (_g(listing, "keyFeatures") or []):
        bits.append(str(k))
    return " ".join(bits).lower()


def agent_questions(score: Any, listing: Any, value: Any = None,
                    taste: Any = None) -> List[Dict[str, str]]:
    """Per-listing "ask the agent" questions from the engine's OWN uncertainty.

    Deterministic rules over structures other layers already computed — the
    listing's tenure/area fields, the value verdict's basis and tag, the taste
    breakdown's weights and contributions. Each question carries the evidence
    that triggered it (``evidence``) and a stable ``trigger`` code, so nothing
    here is an unsourced assertion. ``value``/``taste`` default to the score's
    own sub-objects when a full score.result is passed; the tool layer passes
    its payload dicts directly (both shapes read through ``_g``).

    Returns plain dicts (JSON-ready for the surfaces): ``{question, evidence,
    trigger, kind}`` where ``kind`` is ``ask_agent`` or ``observe``.
    """
    value = value if value is not None else _g(score, "valueVerdict", "value")
    taste = taste if taste is not None else _g(score, "taste")
    out: List[Dict[str, str]] = []

    def add(trigger: str, kind: str, question: str, evidence: str) -> None:
        out.append({"trigger": trigger, "kind": kind,
                    "question": question, "evidence": evidence})

    # Import, never copy (E9): read tenure/areas exactly as the verdict did.
    from gaff_engine import epc as _epc
    from gaff_engine.value import (
        subject_epc_sqft, subject_lease_years, subject_sqft, subject_tenure_type,
    )

    stated = subject_sqft(listing)
    epc_area = subject_epc_sqft(listing)
    blob = _listing_blob(listing)

    # 1 · Claimed works vs the EPC-side area: an EPC that predates a refurb /
    # conversion / extension makes any EPC-derived area suspect — ask for the
    # sign-off and an updated figure rather than assuming either number.
    works = next((p for p in _WORKS_PHRASES if p in blob), None)
    if works and epc_area is not None:
        add("works_vs_epc", "ask_agent",
            "The listing describes works ('%s…'). Is there building-control "
            "sign-off, and an updated EPC / floor area that reflects them?" % works,
            "listing text claims works; the EPC-side area on file is %s sqft "
            "vs %s sqft marketed — the EPC may predate the works"
            % ("{:,.0f}".format(epc_area),
               "{:,.0f}".format(stated) if stated else "an unstated"))

    # 2 · Sqft basis conflict: two areas beyond tolerance → the £/sqft
    # denominator is contested; ask which measurement standard each used.
    basis = _epc.sqft_basis_check(stated, epc_area)
    if basis and basis["conflict"]:
        add("sqft_basis_conflict", "ask_agent",
            "The marketed area and the EPC floor area disagree — which "
            "measurement standard is the marketing figure on (GIA, IPMS, "
            "floorplan gross), and can you share the measured floorplan?",
            "marketing %s sqft vs EPC %s sqft — %.0f%% apart, beyond the "
            "%.1f%% convention tolerance"
            % ("{:,.0f}".format(basis["statedSqft"]),
               "{:,.0f}".format(basis["epcSqft"]), basis["diffPct"],
               _epc.SQFT_BASIS_TOLERANCE_PCT))

    # 3 · Lease: short, or simply unknown on a purchase — either way the cost
    # side of the tenure is a fact only the agent can pin down.
    mode = str(_enum(_g(listing, "mode")) or "buy")
    tenure = subject_tenure_type(listing)
    years = subject_lease_years(listing)
    if mode != "rent":
        if tenure == "leasehold" and years is not None and years < 90:
            add("short_lease", "ask_agent",
                "Confirm the exact unexpired lease term, the ground rent (and "
                "its review schedule), and the service charge with the latest "
                "accounts.",
                "%d years remaining — under the ~90-year line, where extension "
                "cost starts to bite (sharply below 80)" % years)
        elif tenure in ("leasehold", "share_of_freehold") and years is None:
            add("lease_unknown", "ask_agent",
                "What is the unexpired lease term, the ground rent, and the "
                "service charge? The listing states none of them.",
                "tenure is %s but no lease length is given" % tenure)
        elif tenure is None:
            add("tenure_unknown", "ask_agent",
                "Is this freehold or leasehold? If leasehold: unexpired term, "
                "ground rent, service charge.",
                "no tenure stated on a sale listing")

    # 4 · Evidence thinness: the verdict already recorded when it stood on a
    # like-for-like read, a capped tag, or nothing at all — turn that into the
    # agent's burden of proof.
    v_basis = str(_g(value, "basis", default="") or "")
    v_tag = str(_enum(_g(value, "tag")) or "")
    v_err = _g(value, "error")
    if v_err is not None or v_tag == "needs_data":
        add("no_price_evidence", "ask_agent",
            "What comparable sales support this asking price? Ask for "
            "addresses and dates, not a one-line assurance.",
            str(v_err) if v_err is not None else (v_basis or "the engine "
                "returned NEEDS_DATA — it found nothing solid to price against"))
    else:
        if "like-for-like" in v_basis:
            add("llf_only", "ask_agent",
                "What comparable sales — with floor areas — support the price? "
                "The public record here only supports a whole-price comparison.",
                "the verdict is a like-for-like (whole sold price) read: %s"
                % v_basis.split(";")[0])
        if "capped to fair" in v_basis:
            add("thin_comps", "ask_agent",
                "The street has returned few recent sales — what evidence was "
                "the asking price actually set on?",
                "comp-sufficiency gate fired: %s"
                % next((b.strip() for b in v_basis.split(";")
                        if "capped to fair" in b), "few qualifying comps"))

    # 5 · High-weight, thin-evidence taste axes: the axes the score leans on
    # hardest deserve in-person verification when the read's own contribution
    # line was too thin to stand on. Deterministic: weight ≥ 8 of 10, and a
    # contribution under 25 characters (an unevidenced read); at most the top
    # two by weight, so uncertainty stays a signal rather than noise.
    rows = _g(taste, "breakdown") or _g(taste, "axisBreakdown") or []
    thin = []
    for r in rows:
        w = _g(r, "weight")
        contrib = str(_g(r, "contribution") or "").strip()
        if w is not None and float(w) >= 8.0 and len(contrib) < 25:
            thin.append((float(w), str(_enum(_g(r, "axis")))))
    for w, axis in sorted(thin, reverse=True)[:2]:
        add("axis_low_evidence", "observe",
            "In person: %s." % _AXIS_IN_PERSON.get(
                axis, "verify %s yourself — the listing gave little to go on" % axis),
            "%s carries weight %.1f in this profile but the read's evidence "
            "was thin" % (axis, w))

    return out


# ---------------------------------------------------------------------------
# §5.1c — the voice debrief: resolve, record, re-weight.
# ---------------------------------------------------------------------------

def _axis_values(score: Any) -> Dict[str, float]:
    out = {}
    for a in (_g(score, "taste.axisBreakdown") or []):
        out[str(_enum(_g(a, "axis")))] = _g(a, "score")
    return out


def _feedback_from_extraction(record: ViewingRecord, extracted: DebriefExtracted,
                              axis_vals: Dict[str, float], *, feedback_id: str) -> Feedback:
    """Turn the extraction into ONE `feedback@1` (kind:"viewing_note", A11): each
    resolved axis_verify item becomes an axis observation (pass confirms the score's
    value, fail confirms it lower), and the new signals become named-loves /
    anti-signals / area. A viewing note is high-trust → τ = voice_rate."""
    by_id = {it.id: it for it in (record.checklist or [])}
    obs: List[Observation] = []
    primary = None
    for r in (extracted.itemResults or []):
        it = by_id.get(r.localItemId)
        if it is None or it.source != "axis_verify" or not it.axis:
            continue
        assumed = axis_vals.get(it.axis, 7.0)
        value = {"pass": assumed, "partial": max(0.0, assumed - 1.5), "fail": max(0.0, assumed - 3.5)}.get(r.status, assumed)
        obs.append(Observation(axis=it.axis, value=float(value), tau=_VOICE_TAU))
        primary = primary or it.axis

    named, anti, area = [], [], None
    for s in (extracted.newSignals or []):
        if s.kind == "named_love":
            named.append(s.value)
        elif s.kind == "anti_signal":
            anti.append((s.value, -6.0, False))
        elif s.kind == "area":
            area = s.value
    return Feedback(kind="viewing_note", observations=obs, namedLoves=named,
                    antiSignalMentions=anti, area=area, primaryAxis=primary,
                    listingRef=record.listingRef, id=feedback_id)


def debrief(record: ViewingRecord, extracted: DebriefExtracted, person: Any, uncertainty: Any,
            score: Any, *, verdict_agreement: VerdictAgreement, walked_away: bool = False,
            config: Dict[str, Any] = CONFIG, feedback_id: str = "fb_viewing_1",
            at: str = _AT) -> Tuple[ViewingRecord, Any, Any, Any]:
    """Run the debrief (§5.1c) → (record', person', uncertainty', receipt). Resolves
    the checklist, records the verdict-held trust metric + savedFromMistake, and
    re-weights the Person through P4 `apply_feedback` (the ONLY Person write path,
    A11). Returns copies; the inputs are untouched."""
    record = copy.deepcopy(record)
    by_id = {it.id: it for it in (record.checklist or [])}

    # Job 1 — resolve the checklist from the extraction.
    for r in (extracted.itemResults or []):
        it = by_id.get(r.localItemId)
        if it is not None:
            it.status = r.status
            it.result = r.result

    # Job 3 — re-weight the Person (the real P4 write path).
    feedback = _feedback_from_extraction(record, extracted, _axis_values(score), feedback_id=feedback_id)
    person2, uncertainty2, receipt, _interp = apply_feedback(person, feedback, uncertainty, config)

    # Job 2 — the trust metric + the protective receipt.
    must_failed = any(it.priority == "must" and it.status == "fail" for it in record.checklist)
    saved = bool(walked_away and must_failed)

    record.debrief = Debrief(
        transcript=None, extracted=extracted, verdictAgreement=verdict_agreement,
        receipt={"before": receipt.before, "after": receipt.after, "summary": receipt.summary,
                 "clarityDelta": receipt.clarityDelta},
        feedbackRefs=[Ref(id=feedback_id, schemaVersion="feedback@1")],
        recordingRef=None, debriefedAt=at)
    record.verdictAgreement = verdict_agreement
    record.savedFromMistake = saved
    record.status = "debriefed"
    record.updatedAt = at
    return record, person2, uncertainty2, receipt


__all__ = [
    "generate_checklist", "checklist_sorted", "prepare_viewing", "debrief",
    "agent_questions",
]
