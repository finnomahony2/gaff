"""U16 — the generative dashboard assembler (06-dashboards §5.0-§5.4).

The mandate's "the AI picks, orders and narrates" reconciled with determinism:
**selection, zoning, tiering and ordering are a PURE function** of the library +
inputs (:func:`select_components`, the 9-step algorithm §5.3); the *only*
generative output is the single narrated ``{headline, subhead}`` pair, which is
bounded to fields already in the sourced result and **cannot add, drop or
re-order a Slot**. So "the same Search + Person yields the same Component set" and
"the AI narrates" are both true — the library picks the Components, the model
writes the sentences.

Three things live here:

* :data:`BUY_LIBRARY` — the ``componentLibrary@1`` registry (the ten
  ``component.spec@1`` cards, §5.2). The single source of "which Components exist
  and how they behave", not scattered per-mode code.
* :data:`BUY_PROFILE` — the Buy ``mode.profile@1.dashboard`` block (§5.4a): the
  order-bearing ``lead``/``body``/``finalist`` arrays + ``stageEmphasis`` /
  ``stageScope``. The array index is the §5.3 step-7 tie-break.
* :func:`select_components` + :func:`assemble_dashboard` — the deterministic
  resolution and its narrated wrapper, producing a ``dashboard.layout@1``.

The two worked examples (§5.3) are the oracle:
* stage ``browse`` → 2 slots ``[value_verdict(compact), risk_flags]`` (the calm
  feed: the browse-feed discipline drops every non-lead tier-2/3 even though the
  De Beauvoir composite ≥ ``threshold.alert`` makes ``finalist`` true);
* stage ``shortlist`` → 9 slots in ``(zoneRank, profileIndex)`` order, with
  ``negotiation`` dropped by ``stageScope`` and ``commute_isochrone`` kept as a
  ``needs_data`` placeholder (rule 4).

Pure + deterministic: no I/O, no clock, no model call. Mirrors the codebase's
tested-library style (cf. :mod:`gaff_engine.rules` / :mod:`gaff_engine.value`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from gaff_engine.schemas import (
    ComponentInput, ComponentLibrary, ComponentName, ComponentSpec,
    DashboardBlock, DashboardLayout, Expansion, Mode, Narration, Slot, SlotForm,
    SlotState, Stage, WhenShown, Zone,
)

LIBRARY_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# The component library — componentLibrary@1 (§5.2). Ten component.spec@1 cards;
# the Buy set is worked to completion. Compact builder to keep the registry
# legible; every field traces to a §5.2.N card.
# ---------------------------------------------------------------------------

def _spec(component, tier, zone, primitive, inputs, predicate, prose, sources,
          soph, empty, modes, answers=None, cross=False):
    return ComponentSpec(
        component=component, tier=tier, typicalZone=zone, primitive=primitive,
        inputs=[ComponentInput(from_=f, path=p, required=r) for (f, p, r) in inputs],
        whenShown=WhenShown(predicate=predicate, prose=prose),
        sources=list(sources), sophistication=dict(soph), emptyState=empty,
        modes=dict(modes), answers=list(answers or []), crossListing=cross)


_LIBRARY = [
    # 5.2.1 value_verdict — THE Buy differentiator (lead · tier 3).
    _spec(ComponentName.VALUE_VERDICT, 3, Zone.LEAD, ".vv (renderValueVerdict)",
          [("score.result", "valueVerdict.tag", True),
           ("score.result", "valueVerdict.deltaPct", True),
           ("score.result", "valueVerdict.fairEstimate", True),
           ("score.result", "valueVerdict.band", True),
           ("score.result", "valueVerdict.position", True),
           ("score.result", "valueVerdict.evidence[]", True),
           ("score.result", "valueVerdict.confidence", True),
           ("listing", "buy.price", True)],
          "mode in {buy,rent,dream} AND valueVerdict != null",
          "Any Buy/Rent/Dream listing that was scored (not hard-gate-excluded).",
          ["HM Land Registry Price Paid", "listing"],
          {"plain": "pill + delta + one line.",
           "warm": "+ the fair-estimate figure and the band, one evidence line.",
           "forensic": "the comps maths, all evidence rows and confidence drivers."},
          "No sold comps nearby yet — verdict withheld until Land Registry returns 3+.",
          {"buy": "lead", "rent": "body", "invest": "n/a", "dream": "body"},
          answers=["is_it_a_good_buy"]),
    # 5.2.9 risk_flags — the always-on protector (lead · tier 1).
    _spec(ComponentName.RISK_FLAGS, 1, Zone.LEAD, ".win + flag rows",
          [("score.result", "flags[]", True),
           ("listing", "derived.floodRisk", False),
           ("listing", "buy.tenure.leaseYearsRemaining", False)],
          "true",
          "Always — a tier-1 atom present in every mode and stage, incl. the browse feed.",
          ["EA flood", "listing", "forensics"],
          {"plain": "the single most-severe flag.",
           "warm": "all flags with severity chips (serious/watch/info).",
           "forensic": "+ each flag's source field path and the listing/viewing split."},
          "No risk flags raised — nothing the engine caught to check.",
          {"buy": "lead", "rent": "body", "invest": "body", "dream": "finalist"},
          answers=["hidden_faults"]),
    # 5.2.2 taste_breakdown — is it mine? (body · tier 2).
    _spec(ComponentName.TASTE_BREAKDOWN, 2, Zone.BODY, ".axis (eight gauges) + score ring",
          [("score.result", "taste.score", True),
           ("score.result", "taste.axisBreakdown[]", True),
           ("person", "taste.weights", True)],
          "taste != null",
          "Every scored listing (taste leads or co-leads all four Mixes).",
          ["profile.json"],
          {"plain": "the score ring + the single strongest axis line.",
           "warm": "the ring + the top 3 axis gauges with contributions.",
           "forensic": "all eight axis gauges, the paper→eyes prior/final delta, the staging flag."},
          "Taste read pending — score after the next reaction.",
          {"buy": "body", "rent": "body", "invest": "finalist", "dream": "lead"},
          answers=["layout_works"]),
    # 5.2.3 cost_of_ownership — what will it truly cost? (body · tier 2).
    _spec(ComponentName.COST_OF_OWNERSHIP, 2, Zone.BODY, ".tiles + one narrated line",
          [("listing", "buy.serviceCharge", False),
           ("listing", "buy.groundRent", False),
           ("listing", "buy.councilTax.annualEstimate", False),
           ("listing", "buy.epc.current", False),
           ("score.result", "valueVerdict.evidence[lease_adj]", False)],
          "mode == buy AND listing.buy != null",
          "Any Buy listing (leasehold or freehold).",
          ["listing", "EPC register", "HM Land Registry Price Paid"],
          {"plain": "one line: 'about £X a year all-in beyond the mortgage'.",
           "warm": "the stat-tile group (service charge / ground rent / council tax / EPC).",
           "forensic": "+ the amortised lease-extension cost, ground-rent clause, EPC running delta."},
          "Running costs not published — ask the agent for service charge and ground rent.",
          {"buy": "body", "rent": "suppressed", "invest": "finalist", "dream": "finalist"},
          answers=["true_cost"]),
    # 5.2.8 price_history — why reduced / still here? (body · tier 2).
    _spec(ComponentName.PRICE_HISTORY, 2, Zone.BODY, ".tiles + a timeline row",
          [("listing", "buy.priceHistory[]", True),
           ("listing", "buy.daysOnMarket", True),
           ("score.result", "valueVerdict.evidence[reduction,dom]", False)],
          "mode == buy AND (count(buy.priceHistory) > 0 OR buy.daysOnMarket != null)",
          "Any Buy listing with a history or a days-on-market figure.",
          ["listing"],
          {"plain": "one line: 'cut £45k after 29 days, 47 on market'.",
           "warm": "the reduction timeline + DOM-vs-local-median tile.",
           "forensic": "+ every listing event, relist detection, DOM vs the area median."},
          "No price changes recorded — first listing, fresh to market.",
          {"buy": "body", "rent": "n/a", "invest": "n/a", "dream": "n/a"},
          answers=["why_reduced"]),
    # 5.2.5 comps_table — the evidence spine (body · tier 3, finalist-gated).
    _spec(ComponentName.COMPS_TABLE, 3, Zone.BODY, ".dealtable (horizontal scroller)",
          [("listing", "buy.soldComps[]", True),
           ("score.result", "valueVerdict.evidence[comp]", True),
           ("score.result", "valueVerdict.streetMedianPerSqft", False)],
          "finalist == true AND count(buy.soldComps) >= 1",
          "Finalists only (tier 3, data-heavy). Shows however many comps qualified.",
          ["HM Land Registry Price Paid"],
          {"plain": "the count + median line.",
           "warm": "the table: address, sold price, £/sqft, date, distance.",
           "forensic": "+ each comp's recency/distance weight and the 'wanted 5, have N' note."},
          "No qualifying sold comps within range — value read is like-for-like only.",
          {"buy": "body", "rent": "suppressed", "invest": "body", "dream": "finalist"}),
    # P5 support: viewing_checklist — composes flags[kind:viewing] (finalist · tier 3).
    _spec(ComponentName.VIEWING_CHECKLIST, 3, Zone.FINALIST, ".win + checklist rows",
          [("score.result", "flags[kind:viewing]", True),
           ("score.result", "reasons[]", False)],
          "finalist == true",
          "Finalists (tier 3). The viewing half of the dual output — what to check on site.",
          ["forensics", "listing"],
          {"plain": "the count of things to check on site.",
           "warm": "each viewing flag as a checklist row.",
           "forensic": "+ the floorplan/photo source for each and the taste axes to re-judge in person."},
          "Nothing flagged for the viewing yet — the read caught no on-site checks.",
          {"buy": "finalist", "rent": "finalist", "invest": "n/a", "dream": "finalist"}),
    # 5.2.4 commute_isochrone — the canonical needs_data demonstrator (finalist · tier 3).
    _spec(ComponentName.COMMUTE_ISOCHRONE, 3, Zone.FINALIST, ".mapwrap (baked OSM + isochrone)",
          [("derived", "commute[]", True), ("listing", "geo", True)],
          "finalist == true",
          "Finalists only, once the per-search routing has run; else its honest empty state.",
          ["routing API", "baked OSM"],
          {"plain": "'X min to <target> by bike/transit'.",
           "warm": "the isochrone map to the primary target.",
           "forensic": "multi-target isochrones + door-to-door legs."},
          "Commute not yet run — add a destination to see travel times.",
          {"buy": "finalist", "rent": "lead", "invest": "n/a", "dream": "finalist"}),
    # 5.2.10 area_report — the written area market report (finalist · tier 3).
    _spec(ComponentName.AREA_REPORT, 3, Zone.FINALIST, ".win + editorial prose + .tiles",
          [("listing", "geo", True),
           ("score.result", "valueVerdict.streetMedianPerSqft", False)],
          "finalist == true",
          "Finalists only (tier 3). Generated prose over aggregate open data — fully sourced.",
          ["HM Land Registry Price Paid", "EPC register"],
          {"plain": "a two-sentence area summary.",
           "warm": "the summary + a price-trend tile group.",
           "forensic": "+ the £/sqft trend, EPC distribution, turnover rate, each figure sourced."},
          "Area report pending — building the local sold-price picture.",
          {"buy": "finalist", "rent": "finalist", "invest": "finalist", "dream": "finalist"}),
    # P5 support: negotiation — offer-stage only via stageScope (finalist · tier 3).
    _spec(ComponentName.NEGOTIATION, 3, Zone.FINALIST, ".win + lever rows",
          [("score.result", "valueVerdict", True),
           ("listing", "buy.priceHistory[]", False),
           ("score.result", "flags[]", False)],
          "finalist == true",
          "Buy offer stage only (stageScope). Data, not advice (00-frame-bound).",
          ["HM Land Registry Price Paid", "listing"],
          {"plain": "the one strongest lever.",
           "warm": "the levers: over/under fair, reduction, DOM, lease, flags.",
           "forensic": "+ each lever's figure and source, framed as fact not spin."},
          "No negotiation levers yet — needs the value verdict and price history.",
          {"buy": "finalist", "rent": "suppressed", "invest": "finalist", "dream": "suppressed"}),
    # 5.3 support: affordability — the RENT lead (tier 1). £pcm vs the budget band.
    _spec(ComponentName.AFFORDABILITY, 1, Zone.LEAD, ".tiles + one narrated line",
          [("listing", "rent.rentPcm", True),
           ("search", "budget.max", False),
           ("listing", "rent.billsIncluded", False)],
          "mode == rent",
          "The Rent lead — £pcm vs your budget band, the first thing that decides a let.",
          ["listing", "search budget"],
          {"plain": "'£X pcm — within / a stretch / over your budget'.",
           "warm": "the £pcm-vs-band tile + headroom + bills-in.",
           "forensic": "+ £pcm/sqft vs the area, deposit, available-from, the full band."},
          "Rent not published — ask the agent for the pcm.",
          {"buy": "n/a", "rent": "lead", "invest": "n/a", "dream": "n/a"},
          answers=["can_i_afford_it"]),
    # 5.4 support: deal_table — the INVEST lead (cross-listing, routed to assembleFeed).
    _spec(ComponentName.DEAL_TABLE, 3, Zone.LEAD, ".dealtable (cross-listing scroller)",
          [("score.result", "composite", True)],
          "mode == invest",
          "Invest home view — the yield deal-flow table (cross-listing; via assembleFeed, not a per-listing slot).",
          ["HM Land Registry Price Paid", "listing"],
          {"plain": "the ranked deal rows.",
           "warm": "+ the yield / refurb columns.",
           "forensic": "+ cashflow, the walk-aways, each figure sourced."},
          "No deals in range yet — widen the search.",
          {"buy": "n/a", "rent": "n/a", "invest": "lead", "dream": "n/a"}, cross=True),
    # 5.5 support: imagery — the DREAM lead (tier 2). Aspiration, no clock.
    _spec(ComponentName.IMAGERY, 2, Zone.LEAD, ".gallery (hero imagery + vision aspect)",
          [("listing", "images[]", True)],
          "mode == dream",
          "Dream home view — the imagery that trains the eye; the forensics aspect read beside it.",
          ["listing", "forensics vision read"],
          {"plain": "the hero image.",
           "warm": "the gallery + the forensics aspect note.",
           "forensic": "+ every room, the floorplan, the full vision read."},
          "No imagery yet — add photos to see the gallery.",
          {"buy": "finalist", "rent": "n/a", "invest": "n/a", "dream": "lead"}),
]

BUY_LIBRARY = ComponentLibrary(libraryVersion=LIBRARY_VERSION, components=_LIBRARY)

# The Buy mode.profile@1.dashboard block (§5.4a) — order-bearing zone arrays.
BUY_PROFILE = DashboardBlock(
    lead=["value_verdict", "risk_flags"],
    body=["taste_breakdown", "cost_of_ownership", "price_history", "comps_table"],
    finalist=["viewing_checklist", "commute_isochrone", "area_report", "negotiation"],
    suppressed=[],
    stageEmphasis={"viewing": ["viewing_checklist"], "offer": ["negotiation"]},
    stageScope={"negotiation": ["offer"]},
)

# The three deferred mode.profile@1.dashboard blocks (§5.3-5.5), at contract level:
# each leads with its mode's distinct Component, and suppresses what doesn't apply.
# RENT leads with affordability + commute; ownership Components are suppressed.
RENT_PROFILE = DashboardBlock(
    lead=["affordability", "commute_isochrone"],
    body=["taste_breakdown", "value_verdict", "risk_flags"],
    finalist=["viewing_checklist", "area_report"],
    suppressed=["cost_of_ownership", "price_history", "comps_table", "negotiation", "lease_explainer"],
    stageEmphasis={"viewing": ["viewing_checklist"]},
    stageScope={},
)
# INVEST leads with the deal_table (a cross-listing feed surface, §5.4); the
# per-listing dashboard shows the financial verdict + evidence.
INVEST_PROFILE = DashboardBlock(
    lead=["deal_table", "value_verdict", "risk_flags"],
    body=["taste_breakdown", "comps_table"],
    finalist=["area_report"],
    suppressed=["cost_of_ownership", "price_history", "negotiation", "affordability", "commute_isochrone"],
    stageEmphasis={}, stageScope={},
)
# DREAM leads with imagery + taste; no budget, no clock — ownership all suppressed.
DREAM_PROFILE = DashboardBlock(
    lead=["imagery", "taste_breakdown"],
    body=["value_verdict"],
    finalist=["area_report", "commute_isochrone"],
    suppressed=["cost_of_ownership", "price_history", "negotiation", "comps_table", "affordability"],
    stageEmphasis={}, stageScope={},
)

# The four lenses, keyed by Mode — one library, four products (§5.0 / A1).
PROFILES = {"buy": BUY_PROFILE, "rent": RENT_PROFILE, "invest": INVEST_PROFILE, "dream": DREAM_PROFILE}
# Each Mode's default Scorer Mix (§5.0; a Search may override). taste/rules/value.
MODE_MIX = {"buy": (55, 20, 25), "rent": (60, 25, 15), "invest": (30, 15, 55), "dream": (80, 5, 15)}


def profile_for(mode: Any) -> DashboardBlock:
    """The mode.profile@1.dashboard block for a Mode (defaults to Buy)."""
    return PROFILES.get(str(_enum_val(mode)), BUY_PROFILE)


_ZONE_RANK = {"lead": 0, "body": 1, "finalist": 2}


# ---------------------------------------------------------------------------
# Accessors + resolvers (codebase style, cf. rules._g).
# ---------------------------------------------------------------------------

def _g(obj: Any, *names: str, default: Any = None) -> Any:
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


def _enum_val(v: Any) -> Any:
    return getattr(v, "value", v)


def _mode(search: Any) -> str:
    return str(_enum_val(_g(search, "mode", default="buy")))


def _stage(pursuit: Any) -> str:
    if pursuit is None:
        return Stage.BROWSE.value
    return str(_enum_val(pursuit if isinstance(pursuit, (str,)) else _g(pursuit, "stage", default="browse")))


def _spec_by_name(library: ComponentLibrary) -> Dict[str, ComponentSpec]:
    return {_enum_val(c.component): c for c in library.components}


def _threshold_alert(search: Any) -> Optional[float]:
    v = _g(search, "threshold.alert", "alert")
    return float(v) if v is not None else None


# --- availability (step 3): engine-fed cards read score.result.components[]; the
#     presentation cards are ready iff every required input path is present. ------

def _components_availability(score_result: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for c in (_g(score_result, "components", default=[]) or []):
        out[_enum_val(_g(c, "component"))] = _enum_val(_g(c, "availability"))
    return out


def _viewing_flags(score_result: Any) -> List[Any]:
    return [f for f in (_g(score_result, "flags", default=[]) or [])
            if _enum_val(_g(f, "kind")) == "viewing"]


def _availability(comp: str, spec: ComponentSpec, score_result: Any, listing: Any,
                  engine_avail: Dict[str, str]) -> str:
    # Engine-fed components carry their availability on the result.
    if comp in engine_avail:
        return engine_avail[comp]
    # viewing_checklist is ready iff there is at least one viewing flag.
    if comp == "viewing_checklist":
        return SlotState.READY.value if _viewing_flags(score_result) else SlotState.NEEDS_DATA.value
    # negotiation needs the value verdict present.
    if comp == "negotiation":
        return SlotState.READY.value if _g(score_result, "valueVerdict") is not None else SlotState.NEEDS_DATA.value
    # area_report is ready once we have a street £/sqft aggregate to summarise.
    if comp == "area_report":
        return (SlotState.READY.value if _g(score_result, "valueVerdict.streetMedianPerSqft") is not None
                else SlotState.NEEDS_DATA.value)
    # affordability (the Rent lead) is ready once the listing carries a rent £pcm.
    if comp == "affordability":
        return (SlotState.READY.value if _g(listing, "rent.rentPcm") is not None
                else SlotState.NEEDS_DATA.value)
    # Presentation cards: ready iff every required input path resolves on its object.
    ctx = {"listing": listing, "score.result": score_result}
    for inp in (spec.inputs or []):
        if not inp.required:
            continue
        base = ctx.get(inp.from_)
        path = inp.path.split("[")[0]  # ignore the [comp]/[] filter suffix for presence
        if _g(base, path) is None:
            return SlotState.NEEDS_DATA.value
    return SlotState.READY.value


# --- whenShown applicability (step 4). Resolver table keyed by component — the
#     data-presence clauses are handled by availability (step 3), not here, so a
#     needs_data card STAYS as a placeholder (rule 4) rather than being dropped. -

def _when_shown(comp: str, mode: str, listing: Any, score_result: Any,
                finalist: bool, zone: str = "body") -> bool:
    if comp == "value_verdict":
        return mode in ("buy", "rent", "dream", "invest") and _g(score_result, "valueVerdict") is not None
    if comp == "risk_flags":
        return True
    if comp == "taste_breakdown":
        return _g(score_result, "taste") is not None
    if comp == "affordability":
        return mode == "rent"                        # availability decides ready vs needs_data (A1 placeholder)
    if comp == "imagery":
        return mode in ("dream", "buy")
    if comp == "deal_table":
        return mode == "invest"                      # crossListing → routed out per-listing (step 2)
    if comp == "cost_of_ownership":
        return mode == "buy" and _g(listing, "buy") is not None
    if comp == "price_history":
        return mode == "buy" and (
            bool(_g(listing, "buy.priceHistory")) or _g(listing, "buy.daysOnMarket") is not None)
    if comp == "comps_table":
        return finalist and len(_g(listing, "buy.soldComps", default=[]) or []) >= 1
    # viewing_checklist / commute_isochrone / area_report / negotiation: a lead-zone
    # placement (rent's commute) shows; elsewhere they are finalist-gated (the calm
    # feed). Data-presence is an availability (needs_data) concern, not a drop.
    if comp in ("viewing_checklist", "commute_isochrone", "area_report", "negotiation"):
        return True if zone == "lead" else finalist
    return True


# --- tier gate + browse-feed discipline (step 6). Returns (keep, form). -------

def _tier_gate(tier: int, zone: str, browse: bool, finalist: bool) -> Tuple[bool, Optional[str]]:
    if browse:
        # The feed card: only the lead row survives (A4 calm feed). The sole tier-3
        # is the lead Value Verdict, stamped compact BECAUSE the stage is browse.
        if zone != "lead":
            return (False, None)
        if tier == 3:
            return (True, SlotForm.COMPACT.value)
        if tier == 1:
            return (True, SlotForm.FULL.value)
        return (False, None)                     # a lead tier-2 is detail-only (rule 3)
    # Detail view (stage != browse) ⇒ finalist is True.
    if tier == 1 or tier == 2:
        return (True, SlotForm.FULL.value)
    # tier 3 on the detail view: lead verdict full when finalist, others finalist-gated.
    if finalist:
        return (True, SlotForm.FULL.value)
    return (zone == "lead", SlotForm.COMPACT.value if zone == "lead" else None)


# --- sophistication stamp (step 8, §5.5). ------------------------------------

def _sophistication_level(person: Any, mode_profile: Any) -> str:
    default = _g(mode_profile, "sophisticationDefault")
    tone = _g(person, "values.narrationTone", "narrationTone", default=default or "plain")
    return str(tone)


def _expansion(zone: str, tier: int, level: str) -> str:
    if level == "forensic":
        return Expansion.EXPANDED.value if tier >= 2 else Expansion.COLLAPSED_PLUS.value
    if level == "warm":
        return Expansion.EXPANDED.value if zone == "lead" else Expansion.COLLAPSED_PLUS.value
    # plain (default): lead expanded, everything else a one-tap reveal.
    return Expansion.EXPANDED.value if zone == "lead" else Expansion.COLLAPSED_PLUS.value


# ---------------------------------------------------------------------------
# select_components — the deterministic 9-step resolution (§5.3).
# ---------------------------------------------------------------------------

def select_components(score_result: Any, person: Any, search: Any, listing: Any,
                      mode_profile: Any = BUY_PROFILE, pursuit: Any = None,
                      config: Any = None, *, library: ComponentLibrary = BUY_LIBRARY
                      ) -> List[Slot]:
    """The pure selection+ordering resolution: ``(...) -> Slot[]``. For fixed
    ``{library, profile, person tone, search config, pursuit.stage}`` it returns a
    byte-identical ``Slot[]`` (set, zones, tiers, forms, order). No randomness, no
    model call — narration is composed afterward by :func:`assemble_dashboard` and
    cannot alter the returned slots (the rule-5 / determinism invariant).

    ``listing`` is an explicit argument (P5 §5.1 keeps it in scope; the Components'
    ``whenShown``/availability read Listing fields), so this refines P5's
    ``selectComponents(scoreResult, person, search, modeProfile, pursuit, config)``
    with the Listing the predicates need."""
    specs = _spec_by_name(library)
    mode = _mode(search)
    stage = _stage(pursuit)
    browse = (stage == Stage.BROWSE.value)
    engine_avail = _components_availability(score_result)

    # 6 (pre): finalist eligibility — stage ≠ browse OR composite ≥ threshold.alert.
    alert = _threshold_alert(search)
    composite = _g(score_result, "composite")
    finalist = (not browse) or (
        composite is not None and alert is not None and float(composite) >= alert)

    suppressed = set(_g(mode_profile, "suppressed", default=[]) or [])
    stage_scope = _g(mode_profile, "stageScope", default={}) or {}
    level = _sophistication_level(person, mode_profile)

    rows: List[Tuple[int, int, Slot]] = []  # (zoneRank, profileIndex, Slot)
    for zone in ("lead", "body", "finalist"):
        arr = _g(mode_profile, zone, default=[]) or []
        for pidx, comp in enumerate(arr):
            if comp in suppressed:                       # step 1 (rule 1)
                continue
            spec = specs.get(comp)
            if spec is None:
                continue
            if spec.crossListing:                        # step 2 (deal_table route-out)
                continue
            if not _when_shown(comp, mode, listing, score_result, finalist, zone):  # step 4
                continue
            scope = stage_scope.get(comp)                # step 5 — stage scope
            if scope and stage not in scope:
                continue
            keep, form = _tier_gate(spec.tier, zone, browse, finalist)  # step 6
            if not keep:
                continue
            state = _availability(comp, spec, score_result, listing, engine_avail)  # step 3
            slot = Slot(
                component=spec.component, tier=spec.tier, zone=zone, form=form,
                state=state, expansion=_expansion(zone, spec.tier, level),
                sources=list(spec.sources or []),
                reason="%s[%d]; %s" % (zone, pidx, _reason_note(spec.tier, zone, browse, state)))
            rows.append((_ZONE_RANK[zone], pidx, slot))

    # step 7 — order by (zoneRank, profileIndex); then stage emphasis to the lead front.
    rows.sort(key=lambda r: (r[0], r[1]))
    slots = [r[2] for r in rows]
    slots = _apply_stage_emphasis(slots, mode_profile, stage)
    return slots


def _reason_note(tier: int, zone: str, browse: bool, state: str) -> str:
    if browse and zone == "lead" and tier == 3:
        return "rule3 lead-zone tier-3 → compact at browse"
    if tier == 1:
        return "tier-1 always"
    if tier == 2:
        return "tier-2 on detail view"
    if state == SlotState.NEEDS_DATA.value:
        return "finalist tier-3; needs_data → emptyState (rule4)"
    return "finalist unlocks tier-3"


def _apply_stage_emphasis(slots: List[Slot], mode_profile: Any, stage: str) -> List[Slot]:
    emph = (_g(mode_profile, "stageEmphasis", default={}) or {}).get(stage)
    if not emph:
        return slots
    promoted = [s for s in slots if _enum_val(s.component) in emph]
    rest = [s for s in slots if _enum_val(s.component) not in emph]
    # promoted move to the front of the lead run, in the emphasis order.
    promoted.sort(key=lambda s: emph.index(_enum_val(s.component)))
    for s in promoted:
        s.zone = "lead"
        s.reason = "stageEmphasis[%s]; " % stage + s.reason
    return promoted + rest


# ---------------------------------------------------------------------------
# Narration (rule 5) — the ONE generative pair, bounded to sourced fields. A
# deterministic composition here (no live model): it cites the verdict + top flag
# and adds NO claim absent from reasons/flags/valueVerdict (the rule-5 linter).
# ---------------------------------------------------------------------------

def _signed_pct(v: float) -> str:
    """A signed percentage with the U+2212 minus glyph, matching the card grammar."""
    return ("%s%.1f%%" % ("−" if v < 0 else "+", abs(v)))


def _narration(score_result: Any, slots: List[Slot]) -> Narration:
    vv = _g(score_result, "valueVerdict")
    flags = _g(score_result, "flags", default=[]) or []
    tag = _enum_val(_g(vv, "tag")) if vv is not None else None
    head = None
    if vv is not None and tag is not None:
        headline_delta = _g(vv, "headlineDeltaPct")
        delta = _g(vv, "deltaPct")
        if headline_delta is not None and delta is not None and abs(headline_delta - delta) >= 0.5:
            head = "Looks %s, really %s at %s once adjusted." % (
                _signed_pct(headline_delta), tag, _signed_pct(delta))
        else:
            head = "A %s buy — %s vs the fair estimate." % (
                tag, _signed_pct(delta if delta is not None else 0.0))
    # subhead: the single most-severe flag, verbatim source (cites, never editorialises).
    sev_rank = {"serious": 0, "watch": 1, "info": 2}
    top = sorted(flags, key=lambda f: sev_rank.get(_enum_val(_g(f, "severity")), 9))
    sub = None
    if top:
        f0 = top[0]
        sub = _g(f0, "text")
    return Narration(headline=head or "Scored on taste, value and rules.",
                     subhead=sub or "No risk flags raised.")


# ---------------------------------------------------------------------------
# assemble_dashboard — select_components + the one narrated pair (§5.3 wrapper).
# ---------------------------------------------------------------------------

def assemble_dashboard(score_result: Any, person: Any, search: Any, listing: Any, *,
                       mode_profile: Any = BUY_PROFILE, pursuit: Any = None,
                       library: ComponentLibrary = BUY_LIBRARY) -> DashboardLayout:
    """Resolve the ordered ``Slot[]`` (deterministic) and front it with the one
    narrated ``{headline, subhead}`` pair (rule 5). Returns a ``dashboard.layout@1``.

    ``listing`` is passed explicitly (``select_components`` reads the Listing for
    its ``whenShown``/availability); the narration is generated *after* selection
    and cannot change the slot set or order."""
    slots = select_components(score_result, person, search, listing, mode_profile,
                              pursuit, library=library)
    return DashboardLayout(
        mode=Mode(_mode(search)) if _mode(search) in [m.value for m in Mode] else None,
        stage=Stage(_stage(pursuit)),
        sophistication=_sophistication_level(person, mode_profile),
        slots=slots,
        narration=_narration(score_result, slots),
        libraryVersion=_g(library, "libraryVersion"),
    )


__all__ = [
    "LIBRARY_VERSION", "BUY_LIBRARY", "BUY_PROFILE", "RENT_PROFILE", "INVEST_PROFILE",
    "DREAM_PROFILE", "PROFILES", "MODE_MIX", "profile_for",
    "select_components", "assemble_dashboard",
]
