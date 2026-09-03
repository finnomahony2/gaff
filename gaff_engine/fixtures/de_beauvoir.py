"""The golden De Beauvoir fixtures (01-domain §5.5b worked example).

This is the quality oracle from docs/build-plan.md Principle 5: the reconciled
De Beauvoir ``score.result@1`` that genuinely recomputes to taste 8.2 across all
eight axes and composite 7.8 at the Buy mix 55/20/25. Every deterministic unit
is verified against it.

Exports:
    GOLDEN_PERSON          person@1     — Sam, Buy-oriented (§5.1 worked example)
    GOLDEN_SEARCH          search@1     — East London, to buy (§5.2 worked example)
    GOLDEN_LISTING         listing@1    — De Beauvoir maisonette (§5.4 worked example)
    GOLDEN_SCORE_RESULT    score.result@1 — the verdict (§5.5b worked example)
    GOLDEN_COMPONENT_SPEC  component.spec@1 — value_verdict (06-dashboards §5.2)

The per-axis taste scores (the eight rows whose weighted mean is 426.5/54.0 =
7.90) are lifted verbatim from 01-domain §5.5b / 03-engine §5.1.
"""

from __future__ import annotations

from gaff_engine.schemas import (
    Address, Agent, AlertPolicy, Area, Availability, AxisBreakdown, Budget,
    BuyDetails, Chain, Collaborator, ComponentInput, ComponentName,
    ComponentSource, ComponentSpec, ConfidenceReport, CouncilTax, DealBreaker,
    Derived, Epc, Flag, FlagCode, FlagKind, FlagSeverity, FloodRisk, Forensics,
    Gate, GateResult, GeoPoint, GroundRentReview, Listing, Media, Mode, Money,
    MoneyPeriod, Person, PortalId, Preference, PriceEvent, PriceQualifier,
    Privacy, ProfileMeta, Provenance, ProvenanceSource, PropertyType, Reason,
    Ref, Role, RulesResult, ScorerMix, ScoreRequest, ScoreRequestOptions,
    ScoreResult, Search, SoftDock, SourcedComponent, Station, TasteAdjustment,
    TasteAxis, TasteProfile, TasteResult, Tenure, TenureType, Threshold, ValueBand,
    ValueEvidence, ValueTag, ValueVerdict, WhenShown,
)

# --- Stable IDs shared across the fixtures (so cross-refs resolve) ----------
PERSON_ID = "person_01J8ZK9SAMP0000000000000"
SEARCH_ID = "search_01J8ZKBUYLDN000000000001"
LISTING_ID = "listing_01J8ZKDBVOIR00000000001"
SCORE_ID = "score_01J8ZKRESULT000000000001"
COMP_A_ID = "comp_01J8ZKCOMPA0000000000001"
COMP_B_ID = "comp_01J8ZKCOMPB0000000000002"
COMP_C_ID = "comp_01J8ZKCOMPC0000000000003"
FORENSICS_ID = "forensics_01J8ZKDBVOIR000000001"

_PERSON_REF = Ref(id=PERSON_ID, schemaVersion="person@1")


# ---------------------------------------------------------------------------
# Person — person@1 (§5.1).
# ---------------------------------------------------------------------------

GOLDEN_PERSON = Person(
    id=PERSON_ID,
    subject="Sam",
    profile=ProfileMeta(version=3, updated="2026-07-12"),
    lifeStage={
        "household": "sharers",
        "householdMembers": [
            {"role": "self", "name": "Sam"},
            {"role": "housemate", "name": "Alex"},
        ],
        "intent": "mixed",
        "horizonMonths": 9,
        "currentHome": {
            "tenure": "rented", "where": "Southwark", "sqft": 700,
            "baths": 1, "outdoor": False,
            "verdict": "characterful and loved, upgrading on size/baths/outdoor",
        },
        "upgradingOn": ["size", "baths", "outdoor"],
    },
    values={
        "ranked": [
            {"value": "character over finish", "weight0to10": 9,
             "evidence": "8+ scores need distinctiveness; ordinary-nice period caps ~6.5 (round1)"},
            {"value": "honesty over hype", "weight0to10": 8,
             "evidence": "discounts staging himself (round2)"},
            {"value": "space and light over postcode", "weight0to10": 7,
             "evidence": "location-blind by choice across both rounds"},
        ],
        "narrationTone": "plain",
    },
    riskAppetite={
        "leaseYearsFloor": 90,
        "conditionTolerance": "cosmetic",
        "chainTolerance": "short-chain-ok",
        "priceStretchPct": 5,
        "flagSensitivity": "high",
    },
    universalDealBreakers=[
        DealBreaker(code="cheap_careless_spec",
                    label="Cheap/careless flip (laminate, grey landlord refurb)",
                    kind="gate", scope="universal",
                    evidence="profile.json anti_signals 'kill'"),
        DealBreaker(code="bad_street_scene", label="Bad street scene",
                    kind="gate", scope="universal",
                    evidence="profile.json 'bad street scene (kill)'"),
    ],
    taste=TasteProfile(
        # profile.json v3 weights verbatim; Sigma = 54.0.
        weights={
            "light_and_volume": 10, "outdoor_space": 9, "character_bones": 8.5,
            "width_proportion_flow": 8, "street_scene": 8, "raw_size_threshold": 6,
            "design_finish": 4, "station_proximity": 0.5,
        },
        lovesNamed=[
            "double-fronted width", "curved bay / curved triple glazing",
            "double-height spaces", "wisteria/kerb planting", "exposed brick",
            "colour-drenched period rooms", "Crittall-style glazing",
            "skylit kitchens", "big terraces on penthouses", "conservatories",
        ],
        rules={
            "new_build_cap": "soft ceiling 6.5-7 WITH exceptional-view/terrace exception",
            "condition_axis_split": "kill CHEAP/CARELESS; AGED/ECLECTIC/WARM with bones is neutral-to-positive",
            "separate_living_room": "open-plan-only = dock",
            "price_within_band": "gate + tiebreak only, never a scoring input",
            "size_threshold_enforced": "no bonus above ~1700 sqft",
        },
        hardConstraintsDefault={"minBeds": 2, "minBaths": 2, "minSqft": 900,
                                "outdoor": "required-private-preferred"},
    ),
    privacy=Privacy(exportable=True, retention="user-controlled",
                    lastExportedAt=None, deletedAt=None),
    calibration={
        "round1": {"date": "2026-07-12", "mae": 1.27, "spearman": 0.77, "engine": "text-only"},
        "round2": {"date": "2026-07-12", "mae": 1.35, "spearman": 0.79, "engine": "text+images"},
    },
)


# ---------------------------------------------------------------------------
# Search — search@1 (§5.2). Buy mix 55/20/25.
# ---------------------------------------------------------------------------

GOLDEN_SEARCH = Search(
    id=SEARCH_ID,
    personRef=_PERSON_REF,
    title="East London, to buy",
    mode=Mode.BUY,
    budget=Budget(
        min=Money(amount=1000000, currency="GBP", period=MoneyPeriod.TOTAL),
        max=Money(amount=1350000, currency="GBP", period=MoneyPeriod.TOTAL),
        stretchMax=Money(amount=1417500, currency="GBP", period=MoneyPeriod.TOTAL),
    ),
    area=Area(
        label="East London polygon (De Beauvoir -> Clapton -> Victoria Park)",
        confidence="rough",
        polygon=[[-0.088, 51.545], [-0.052, 51.549], [-0.031, 51.540],
                 [-0.043, 51.522], [-0.077, 51.520], [-0.093, 51.531],
                 [-0.088, 51.545]],
    ),
    gates=[
        Gate(code="min_beds", op=">=", value=2, rationale="Person.taste.hardConstraintsDefault"),
        Gate(code="min_baths", op=">=", value=2, rationale="Person.taste.hardConstraintsDefault"),
        Gate(code="min_sqft", op=">=", value=900, unit="sqft", rationale="size gate; layout is the real gate"),
        Gate(code="outdoor_present", op="==", value=True, rationale="outdoor required; shared-only = -3 penalty not fail"),
        Gate(code="lease_years_min", op=">=", value=90, unit="years", soft=True,
             rationale="Person.riskAppetite.leaseYearsFloor -- sub-90 flags, does not auto-exclude"),
        Gate(code="tenure_in", op="in", value=["freehold", "share_of_freehold", "leasehold"]),
        Gate(code="inside_polygon", op="geo_within", value="area.polygon"),
    ],
    preferences=[
        Preference(code="area_tighten", weightDelta=1.5,
                   value=["De Beauvoir", "London Fields", "Victoria Park"],
                   note="nudge, not gate"),
    ],
    scorerMix=ScorerMix(taste=55, rules=20, value=25),
    threshold=Threshold(show=6.0, alert=7.5),
    alertPolicy=AlertPolicy(channel="email", cadence="daily", minComposite=7.5,
                            maxPerDigest=8, quietHours="22:00-07:00"),
    collaborators=[
        Collaborator(email="owner@example.com", role=Role.OWNER, personRef=_PERSON_REF),
        Collaborator(email="editor@example.com", role=Role.EDITOR),
    ],
    provenance=Provenance(source=ProvenanceSource.FORWARDED_ALERT_EMAIL, portal="example",
                          fetchedAt="2026-07-13T08:12:00Z", freshness="fresh", isDemo=False),
    status="active",
    createdAt="2026-07-13T08:00:00Z",
    updatedAt="2026-07-13T08:12:00Z",
)


# ---------------------------------------------------------------------------
# Listing — listing@1 (§5.4). De Beauvoir Victorian maisonette.
# ---------------------------------------------------------------------------

GOLDEN_LISTING = Listing(
    id=LISTING_ID,
    listingKey="0000000000000000000000000000000000000001",
    portalIds=[PortalId(portal="example", id="10000001",
                        url="https://listings.example.com/10000001")],
    mode=Mode.BUY,
    address=Address(display="Northchurch Road, De Beauvoir, London N1",
                    line1="Northchurch Road", outcode="N1", incode="6AA",
                    postcode="N1 6AA", ukCountry="England"),
    geo=GeoPoint(lat=51.5423, lng=-0.0851, accuracy="accurate"),
    propertyType=PropertyType.MAISONETTE,
    beds=2,
    baths=2,
    receptions=2,
    sqft=1050,
    sqm=97.5,
    # Synthetic particulars: written for this fixture, not copied from a portal.
    # Tuned so the deterministic keyword fallback in taste._named_loves finds the
    # same three loves the canonical recording asserts (skylit / bay / kerb
    # planting) and no fourth -- note "terrace" is a substring of the
    # "big terraces on penthouses" love, so it is kept out of the blob.
    keyFeatures=[
        "Raised ground and lower ground maisonette", "Private south-west garden",
        "Two double bedrooms, two bathrooms", "Period features and a bay window",
        "Share of freehold", "Chain free",
    ],
    description=("A characterful two-bedroom period maisonette arranged over the raised "
                 "and lower ground floors of a handsome Victorian townhouse, with a "
                 "private south-west facing garden, restored floorboards, mature kerb "
                 "planting to the front, a bay window to the double reception and a "
                 "skylit kitchen."),
    images=[Media(url="https://media.example.com/photo01.jpg", kind="photo",
                  caption="Bay window reception")],
    floorplans=[Media(url="https://media.example.com/floorplan.gif", kind="floorplan",
                      caption="Floorplan 1")],
    nearestStations=[
        Station(name="Dalston Junction", types=["OVERGROUND"], distanceMiles=0.5),
        Station(name="Canonbury", types=["OVERGROUND"], distanceMiles=0.6),
    ],
    agent=Agent(branchName="Hackney", companyName="Example & Co", url="/estate-agents/"),
    buy=BuyDetails(
        price=Money(amount=1150000, currency="GBP", period=MoneyPeriod.TOTAL,
                    qualifier=PriceQualifier.GUIDE),
        tenure=Tenure(type=TenureType.LEASEHOLD, leaseYearsRemaining=89),
        groundRent=Money(amount=150, currency="GBP", period=MoneyPeriod.PA),
        groundRentReview=GroundRentReview(periodYears=25, percentIncrease=0),
        serviceCharge=Money(amount=1400, currency="GBP", period=MoneyPeriod.PA),
        epc=Epc(current=62, rating="D", potential=79),
        councilTax=CouncilTax(band="E", annualEstimate=Money(amount=2400, currency="GBP", period=MoneyPeriod.PA)),
        priceHistory=[
            PriceEvent(date="2026-05-27", event="listed",
                       price=Money(amount=1195000, currency="GBP", period=MoneyPeriod.TOTAL)),
            PriceEvent(date="2026-06-25", event="reduced",
                       price=Money(amount=1150000, currency="GBP", period=MoneyPeriod.TOTAL)),
        ],
        daysOnMarket=47,
        chain=Chain.CHAIN_FREE,
        newBuild=False,
        soldComps=[
            Ref(id=COMP_A_ID, schemaVersion="comp@1"),
            Ref(id=COMP_B_ID, schemaVersion="comp@1"),
            Ref(id=COMP_C_ID, schemaVersion="comp@1"),
        ],
    ),
    derived=Derived(
        forensics=Ref(id=FORENSICS_ID, schemaVersion="forensics@1"),
        pricePerSqft=1095,
        floodRisk=FloodRisk(band="very_low", source="environment_agency"),
    ),
    provenance=Provenance(
        source=ProvenanceSource.FORWARDED_ALERT_EMAIL, portal="example",
        fetchedAt="2026-07-13T08:10:00Z", freshness="fresh", isDemo=False,
        completeness={"sqft": "stated", "epc": "register",
                      "soldComps": "3_of_min_5", "leaseYears": "stated"},
    ),
)


# ---------------------------------------------------------------------------
# Forensics — forensics@1 (§5.5). The De Beauvoir vision read (Person-independent,
# once per listingKey). Reproduces §5.5's worked example: SW-rear aspect, generous
# bay ceiling, no HMO tells, no cheap-flip signals, raised+lower-ground floor read
# (which feeds the `lower_ground_light` viewing flag in §5.5b's ScoreResult).
# ---------------------------------------------------------------------------

GOLDEN_FORENSICS = Forensics(
    id=FORENSICS_ID,
    listingKey=GOLDEN_LISTING.listingKey,
    roomWidthsM=[3.8, 4.2],                 # bay reception + kitchen — not skinny
    walkThroughBedroom=False,
    hmoTells=False,
    cheapFlipSignals=[],                    # restored boards, no white-box flip
    aspect="south-west (rear)",
    ceilingHeightCue="generous (bay)",
    floorPosition="raised + lower ground",  # -> the lower_ground_light watch flag
    imageSetHash="img_golden_v1",
)


# ---------------------------------------------------------------------------
# ScoreResult — score.result@1 (§5.5b). THE golden verdict.
# ---------------------------------------------------------------------------

# The eight taste axis rows (verbatim §5.5b / 03-engine §5.1):
#   Sigma weight = 54.0, Sigma(score*weight) = 426.5, base = 426.5/54.0 = 7.90.
GOLDEN_AXIS_BREAKDOWN = [
    AxisBreakdown(axis=TasteAxis.LIGHT_AND_VOLUME, score=8.0, weight=10,
                  contribution="bay + skylit kitchen; lower-ground darkness risk to check"),
    AxisBreakdown(axis=TasteAxis.OUTDOOR_SPACE, score=8.5, weight=9,
                  contribution="private SW garden with inside-outside potential"),
    AxisBreakdown(axis=TasteAxis.CHARACTER_BONES, score=9.0, weight=8.5,
                  contribution="Victorian bones, restored floorboards, bay -- distinctive not ordinary-nice"),
    AxisBreakdown(axis=TasteAxis.WIDTH_PROPORTION_FLOW, score=7.0, weight=8,
                  contribution="single-fronted terrace; flows over two floors, not double-fronted"),
    AxisBreakdown(axis=TasteAxis.STREET_SCENE, score=8.0, weight=8,
                  contribution="handsome De Beauvoir terrace"),
    AxisBreakdown(axis=TasteAxis.RAW_SIZE_THRESHOLD, score=7.0, weight=6,
                  contribution="1050 sqft -- comfortably over the 900 gate, no size bonus territory"),
    AxisBreakdown(axis=TasteAxis.DESIGN_FINISH, score=7.0, weight=4,
                  contribution="restored boards, skylit kitchen, no marble"),
    AxisBreakdown(axis=TasteAxis.STATION_PROXIMITY, score=7.0, weight=0.5,
                  contribution="Dalston Junction 0.5 mi"),
]

# The one adjustment: +0.30 named-love (3 hits x 0.1, under the 0.5 cap).
GOLDEN_TASTE_ADJUSTMENTS = [
    TasteAdjustment(kind="named_love", delta=0.3,
                    source="skylit kitchen · kerb planting · bay"),
]

GOLDEN_SCORE_RESULT = ScoreResult(
    id=SCORE_ID,
    request=ScoreRequest(
        listingRef=Ref(id=LISTING_ID, schemaVersion="listing@1"),
        personRef=_PERSON_REF,
        searchRef=Ref(id=SEARCH_ID, schemaVersion="search@1"),
        personVersionSnapshot=3,
        searchConfigHash="cfg_9b21",
    ),
    composite=7.8,
    taste=TasteResult(
        score=8.2,
        prior=7.4,
        staged=False,
        axisBreakdown=GOLDEN_AXIS_BREAKDOWN,
        tasteAdjustments=GOLDEN_TASTE_ADJUSTMENTS,
    ),
    valueVerdict=ValueVerdict(
        score=7.2,
        tag=ValueTag.FAIR,
        deltaPct=-8.2,
        headlineDeltaPct=-11.7,
        fairEstimate=1302000,
        band=ValueBand(low=1140000, high=1410000),
        position=0.22,
        streetMedianPerSqft=1240,
        basis="3 HM Land Registry comps within 0.2mi, £/sqft, lease-adjusted",
        evidence=[
            ValueEvidence(kind="ppsf", label="This listing £/sqft", value=1095),
            ValueEvidence(kind="comp", label="Comp median £/sqft (Ufton/Northchurch)",
                          value=1240, compRef=Ref(id=COMP_A_ID, schemaVersion="comp@1")),
            ValueEvidence(kind="reduction", label="Reduced £1,195k->£1,150k after 29 days", value=-45000),
            ValueEvidence(kind="dom", label="47 days on market vs ~30 local median", value=47),
            ValueEvidence(kind="lease_adj", label="89-yr lease extension est.", value=-45000,
                          text="brings effective £/sqft to ~1138; the headline steal is really a fair deal"),
        ],
        confidence=0.62,
    ),
    rules=RulesResult(
        score=7.5,
        gatesPassed=True,
        excluded=False,
        gateResults=[
            GateResult(code="min_beds", passed=True),
            GateResult(code="min_baths", passed=True),
            GateResult(code="min_sqft", passed=True),
            GateResult(code="outdoor_present", passed=True),
            GateResult(code="lease_years_min", passed=False, soft=True),
            GateResult(code="inside_polygon", passed=True),
        ],
        softDocks=[SoftDock(rule="lease_years_min (soft)", delta=-0.5)],
    ),
    reasons=[
        Reason(scorer="taste", polarity="+",
               text="His archetype-A: broad Victorian maisonette, own outdoor, restored bones, bay window."),
        Reason(scorer="taste", polarity="+",
               text="Skylit kitchen and SW garden hit two named loves (skylit kitchens, kerb/garden planting)."),
        Reason(scorer="value", polarity="+",
               text="£1,095/sqft against a £1,240 local comp median -- under the street on headline."),
        Reason(scorer="value", polarity="−",
               text="The 89-year lease is why it's cheap; extension (~£45k) turns the apparent steal into a fair deal.",
               evidenceRefs=["lease_adj"]),
        Reason(scorer="rules", polarity="−",
               text="Single-fronted, not the double-fronted width he prizes; caps taste short of a 9."),
    ],
    flags=[
        Flag(code=FlagCode.SHORT_LEASE, severity=FlagSeverity.SERIOUS,
             text="89 years -- under his 90-yr floor and the sub-90 lending/marriage-value line. Budget an extension.",
             kind=FlagKind.LISTING, source="tenure.leaseYearsRemaining"),
        Flag(code=FlagCode.LOWER_GROUND_LIGHT, severity=FlagSeverity.WATCH,
             text="Lower-ground floor -- check daylight in the rear rooms at viewing.",
             kind=FlagKind.VIEWING, source="forensics"),
        Flag(code=FlagCode.EPC_BELOW_C, severity=FlagSeverity.INFO,
             text="EPC D (62); potential C (79). Not a blocker, a negotiation lever.",
             kind=FlagKind.LISTING, source="buy.epc"),
    ],
    confidence=ConfidenceReport(
        overall=0.71, taste=0.80, value=0.62, rules=0.85,
        drivers=["images + floorplan present", "3 close comps"],
        missing=["only 3 comps (want >=5)", "no commute run yet", "street-scene axis unverified"],
    ),
    components=[
        SourcedComponent(component=ComponentName.VALUE_VERDICT, availability=Availability.READY,
                         sources=[ComponentSource(label="HM Land Registry Price Paid", freshness="2026-06-30")]),
        SourcedComponent(component=ComponentName.COMPS_TABLE, availability=Availability.READY,
                         sources=[ComponentSource(label="HM Land Registry Price Paid")]),
        SourcedComponent(component=ComponentName.LEASE_EXPLAINER, availability=Availability.READY,
                         sources=[ComponentSource(label="listing tenure")]),
        SourcedComponent(component=ComponentName.TASTE_BREAKDOWN, availability=Availability.READY,
                         sources=[ComponentSource(label="profile.json v3")]),
        SourcedComponent(component=ComponentName.RISK_FLAGS, availability=Availability.READY,
                         sources=[ComponentSource(label="EA flood + tenure + forensics")]),
        SourcedComponent(component=ComponentName.COMMUTE_ISOCHRONE, availability=Availability.NEEDS_DATA,
                         sources=[ComponentSource(label="TravelTime (not yet run)")]),
    ],
    scoredAt="2026-07-13T08:11:30Z",
)


# ---------------------------------------------------------------------------
# ComponentSpec — component.spec@1 (06-dashboards §5.2, value_verdict).
# ---------------------------------------------------------------------------

GOLDEN_COMPONENT_SPEC = ComponentSpec(
    component=ComponentName.VALUE_VERDICT,
    tier=3,
    typicalZone="lead",
    primitive=".vv (renderValueVerdict)",
    inputs=[
        ComponentInput(from_="score.result", path="valueVerdict", required=True),
        ComponentInput(from_="score.result", path="valueVerdict.evidence", required=True),
    ],
    whenShown=WhenShown(predicate="mode in {buy,rent,dream} AND valueVerdict != null",
                        prose="Show whenever a value verdict exists."),
    sources=["HM Land Registry Price Paid", "listing"],
    answers=["is_it_a_good_buy"],
    sophistication={
        "plain": "the tag + one line",
        "warm": "tag, delta and the headline-vs-adjusted story",
        "forensic": "the comps table, £/sqft maths and confidence drivers expanded",
    },
    emptyState="No sold comps nearby yet -- verdict withheld until Land Registry returns 3+.",
    modes={"buy": "lead", "rent": "body", "invest": "suppressed", "dream": "body"},
    crossListing=False,
)


__all__ = [
    "GOLDEN_PERSON", "GOLDEN_SEARCH", "GOLDEN_LISTING", "GOLDEN_FORENSICS",
    "GOLDEN_SCORE_RESULT", "GOLDEN_COMPONENT_SPEC", "GOLDEN_AXIS_BREAKDOWN",
    "GOLDEN_TASTE_ADJUSTMENTS",
]
