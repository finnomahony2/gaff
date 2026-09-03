"""Gaff engine — core data contracts as Python dataclasses (U1 / Milestone M0).

Faithful, type-hinted encodings of the schemas defined in
``docs/spec/01-domain.md`` §5 (the authority) plus the two additive P3
fields that 01-domain §9 folds in (the four ``valueVerdict`` gauge fields,
``taste.tasteAdjustments[]``, ``rules.excluded``, ``ConfidenceReport.rules``).

Design notes
------------
* ``from __future__ import annotations`` makes every annotation a string, so
  field order is unconstrained and forward references between classes resolve
  lazily via ``typing.get_type_hints`` (used by :mod:`gaff_engine.validate`).
* Every field carries a default so construction is keyword-driven and order is
  free. "Required" (the §5 ``Req ✓`` columns) is encoded by a *non-Optional*
  type hint: a missing required field is ``None`` and the validator flags it.
  Genuinely optional fields are typed ``Optional[...]``.
* Enums are the closed lists of 01-domain §5.8. They mix in ``str`` so an
  instance is also its wire value (``Mode.BUY == "buy"``), which keeps the
  fixtures readable and JSON-friendly.
* Two domain field names collide with Python keywords and are renamed with the
  PEP 8 trailing-underscore convention, noted inline:
  ``GateResult.pass`` -> ``passed``, ``ComponentInput.from`` -> ``from_``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enum registry (01-domain §5.8) — closed lists, `unknown`/`other` reserved.
# ---------------------------------------------------------------------------

class MoneyPeriod(str, Enum):
    TOTAL = "total"
    PCM = "pcm"
    PW = "pw"
    PA = "pa"


class PriceQualifier(str, Enum):
    ASKING = "asking"
    GUIDE = "guide"
    OFFERS_OVER = "offers_over"
    POA = "poa"
    AUCTION = "auction"


class Mode(str, Enum):
    """Search mission type (glossary: Mode). Buy is the built slice."""
    RENT = "rent"
    BUY = "buy"
    INVEST = "invest"
    DREAM = "dream"


class SearchStatus(str, Enum):
    """Status(Search): active | paused | archived | draft."""
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"
    DRAFT = "draft"


class TenureType(str, Enum):
    FREEHOLD = "freehold"
    LEASEHOLD = "leasehold"
    SHARE_OF_FREEHOLD = "share_of_freehold"
    COMMONHOLD = "commonhold"
    UNKNOWN = "unknown"


class PropertyType(str, Enum):
    FLAT = "flat"
    MAISONETTE = "maisonette"
    TERRACED = "terraced"
    SEMI_DETACHED = "semi_detached"
    DETACHED = "detached"
    END_TERRACE = "end_terrace"
    CONVERSION = "conversion"
    WAREHOUSE = "warehouse"
    OTHER = "other"


class ValueTag(str, Enum):
    """The Value Verdict tag (glossary: Value Verdict)."""
    STEAL = "steal"
    FAIR = "fair"
    OVER = "over"
    NEEDS_DATA = "needs_data"   # A2: honest empty-state — can't price (no sqft / ask / comps)


class Chain(str, Enum):
    CHAIN_FREE = "chain_free"
    ONWARD_CHAIN = "onward_chain"
    UNKNOWN = "unknown"


class FlagSeverity(str, Enum):
    INFO = "info"
    WATCH = "watch"
    SERIOUS = "serious"


class FlagKind(str, Enum):
    LISTING = "listing"
    VIEWING = "viewing"


class FlagCode(str, Enum):
    """Open/additive in the spec; the documented members are enumerated here."""
    SHORT_LEASE = "short_lease"
    DOUBLING_GROUND_RENT = "doubling_ground_rent"
    FLOOD_ZONE = "flood_zone"
    NORTH_FACING = "north_facing"
    BUSY_ROAD = "busy_road"
    LOWER_GROUND_LIGHT = "lower_ground_light"
    EPC_BELOW_C = "epc_below_c"
    HMO_HISTORY = "hmo_history"
    CHEAP_CARELESS_SPEC = "cheap_careless_spec"
    BAD_STREET_SCENE = "bad_street_scene"
    STAGING_INFLATION = "staging_inflation"
    NEW_BUILD_CAP = "new_build_cap"


class TasteAxis(str, Enum):
    """The eight taste axes (weights live on Person.taste.weights)."""
    LIGHT_AND_VOLUME = "light_and_volume"
    OUTDOOR_SPACE = "outdoor_space"
    CHARACTER_BONES = "character_bones"
    WIDTH_PROPORTION_FLOW = "width_proportion_flow"
    STREET_SCENE = "street_scene"
    RAW_SIZE_THRESHOLD = "raw_size_threshold"
    DESIGN_FINISH = "design_finish"
    STATION_PROXIMITY = "station_proximity"


class Role(str, Enum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class FeedbackKind(str, Enum):
    CORRECTION = "correction"
    SWIPE = "swipe"
    RATING = "rating"
    VIEWING_NOTE = "viewing_note"


class ProvenanceSource(str, Enum):
    FORWARDED_ALERT_EMAIL = "forwarded_alert_email"
    PASTE_LINK = "paste_link"
    PARTNER_FEED = "partner_feed"
    LAND_REGISTRY = "land_registry"
    EPC_REGISTER = "epc_register"
    ENVIRONMENT_AGENCY = "environment_agency"
    DEMO = "demo"


class Availability(str, Enum):
    """SourcedComponent availability (score.result@1 components[])."""
    READY = "ready"
    NEEDS_DATA = "needs_data"


class Zone(str, Enum):
    """component.spec@1 typicalZone (06-dashboards §5.0)."""
    LEAD = "lead"
    BODY = "body"
    FINALIST = "finalist"


class ComponentName(str, Enum):
    """The score.result@1 components[].component enum (01-domain §5.5b)."""
    VALUE_VERDICT = "value_verdict"
    COMPS_TABLE = "comps_table"
    TASTE_BREAKDOWN = "taste_breakdown"
    RISK_FLAGS = "risk_flags"
    AFFORDABILITY = "affordability"
    LEASE_EXPLAINER = "lease_explainer"
    COMMUTE_ISOCHRONE = "commute_isochrone"
    AREA_REPORT = "area_report"
    COST_OF_OWNERSHIP = "cost_of_ownership"
    PRICE_HISTORY = "price_history"
    NEGOTIATION = "negotiation"
    VIEWING_CHECKLIST = "viewing_checklist"
    DEAL_TABLE = "deal_table"
    IMAGERY = "imagery"
    YIELD_CASHFLOW = "yield_cashflow"
    STREET_SCENE = "street_scene"


# ---------------------------------------------------------------------------
# Shared vocabulary types (01-domain §5.0 / §5.7).
# ---------------------------------------------------------------------------

@dataclass
class Money:
    """Never a display string (01-domain §5.0). Integer major units."""
    amount: int = None
    currency: str = "GBP"
    period: MoneyPeriod = MoneyPeriod.TOTAL
    qualifier: Optional[PriceQualifier] = None  # only on buy.price


@dataclass
class GeoPoint:
    lat: float = None
    lng: float = None
    accuracy: Optional[str] = None  # "accurate" | "approximate"


@dataclass
class Ref:
    """A cross-object link: {id, schemaVersion}, never an embedded copy."""
    id: str = None
    schemaVersion: str = None


@dataclass
class Provenance:
    source: ProvenanceSource = None
    fetchedAt: str = None
    freshness: str = None  # fresh | stale | unknown
    isDemo: bool = None
    portal: Optional[str] = None
    # completeness maps a field name to a FREE-FORM label (not a closed enum).
    completeness: Optional[Dict[str, str]] = None


# ---------------------------------------------------------------------------
# Person — person@1 (01-domain §5.1).
# ---------------------------------------------------------------------------

@dataclass
class ProfileMeta:
    version: int = None
    updated: str = None


@dataclass
class AntiSignal:
    signal: str = None
    penalty: float = None
    fatal: bool = None


@dataclass
class DealBreaker:
    code: str = None
    label: str = None
    kind: str = None   # "gate"
    scope: str = None  # "universal"
    evidence: Optional[str] = None


@dataclass
class TasteProfile:
    """The taste DNA, normalised from profile.json v3 (01-domain §5.1a)."""
    weights: Dict[str, float] = None            # axis -> 0..10 weight
    scoringNotes: Optional[Dict[str, str]] = None
    antiSignals: Optional[List[AntiSignal]] = None
    archetypeTiers: Optional[Dict[str, List[str]]] = None
    lovesNamed: Optional[List[str]] = None
    areaAffinities: Optional[Dict[str, List[str]]] = None
    rules: Optional[Dict[str, str]] = None
    hardConstraintsDefault: Optional[Dict[str, Any]] = None


@dataclass
class Privacy:
    exportable: bool = None
    retention: str = None
    lastExportedAt: Optional[str] = None
    deletedAt: Optional[str] = None


@dataclass
class Person:
    id: str = None
    schemaVersion: str = "person@1"
    subject: str = None
    profile: ProfileMeta = None
    lifeStage: Dict[str, Any] = None      # §5.1 "object"
    values: Dict[str, Any] = None         # §5.1 "object"
    riskAppetite: Dict[str, Any] = None   # §5.1 "object"
    universalDealBreakers: List[DealBreaker] = None
    taste: TasteProfile = None
    privacy: Privacy = None
    calibration: Optional[Dict[str, Any]] = None
    corrections: Optional[List[Ref]] = None


# ---------------------------------------------------------------------------
# Search — search@1 (01-domain §5.2).
# ---------------------------------------------------------------------------

@dataclass
class Gate:
    code: str = None
    op: str = None
    value: Any = None          # int | bool | list | str, per code
    unit: Optional[str] = None
    rationale: Optional[str] = None
    soft: Optional[bool] = None  # soft:true => flag + dock, not exclude


@dataclass
class Preference:
    code: str = None
    weightDelta: Optional[float] = None
    value: Any = None
    note: Optional[str] = None


@dataclass
class ScorerMix:
    """Weights across the three scorers, summing to 100 (Buy = 55/20/25)."""
    taste: float = None
    rules: float = None
    value: float = None


@dataclass
class Threshold:
    show: float = None
    alert: float = None


@dataclass
class Budget:
    """Buy band: min/max/stretchMax Money."""
    min: Money = None
    max: Money = None
    stretchMax: Optional[Money] = None


@dataclass
class Area:
    label: str = None
    confidence: str = None                 # "firm" | "rough"
    polygon: List[List[float]] = None      # [[lng, lat], ...]


@dataclass
class AlertPolicy:
    channel: str = None                    # email | in_app | digest
    cadence: str = None                    # instant | daily | weekly
    minComposite: Optional[float] = None
    maxPerDigest: Optional[int] = None
    quietHours: Optional[str] = None


@dataclass
class Collaborator:
    email: str = None
    role: Role = None
    personRef: Optional[Ref] = None


@dataclass
class Search:
    id: str = None
    schemaVersion: str = "search@1"
    personRef: Ref = None
    title: str = None
    mode: Mode = None
    gates: List[Gate] = None
    scorerMix: ScorerMix = None
    threshold: Threshold = None
    alertPolicy: AlertPolicy = None
    provenance: Provenance = None
    status: SearchStatus = None
    budget: Budget = None
    area: Area = None
    createdAt: str = None
    updatedAt: str = None
    preferences: Optional[List[Preference]] = None
    collaborators: Optional[List[Collaborator]] = None


# ---------------------------------------------------------------------------
# Listing — listing@1 (01-domain §5.4). Buy attributes lead.
# ---------------------------------------------------------------------------

@dataclass
class PortalId:
    portal: str = None
    id: str = None
    url: Optional[str] = None


@dataclass
class Address:
    display: str = None
    outcode: str = None
    ukCountry: str = None
    line1: Optional[str] = None
    line2: Optional[str] = None
    line3: Optional[str] = None
    line4: Optional[str] = None
    incode: Optional[str] = None
    postcode: Optional[str] = None


@dataclass
class Media:
    url: str = None
    kind: str = None             # "photo" | "floorplan"
    caption: Optional[str] = None


@dataclass
class Station:
    name: str = None
    types: List[str] = None
    distanceMiles: float = None


@dataclass
class Agent:
    branchName: str = None
    companyName: str = None
    phone: Optional[str] = None
    url: Optional[str] = None


@dataclass
class Tenure:
    type: TenureType = None
    leaseYearsRemaining: Optional[int] = None


@dataclass
class GroundRentReview:
    periodYears: int = None
    percentIncrease: float = None


@dataclass
class Epc:
    current: Optional[int] = None
    rating: Optional[str] = None       # "A".."G"
    potential: Optional[int] = None


@dataclass
class CouncilTax:
    band: Optional[str] = None         # A..H
    annualEstimate: Optional[Money] = None


@dataclass
class PriceEvent:
    date: str = None
    event: str = None                  # listed | reduced | increased | sold_stc
    price: Money = None


@dataclass
class BuyDetails:
    """The truth-layer surface; required when Listing.mode == buy."""
    price: Money = None
    tenure: Tenure = None
    groundRent: Optional[Money] = None
    groundRentReview: Optional[GroundRentReview] = None
    serviceCharge: Optional[Money] = None
    epc: Optional[Epc] = None
    councilTax: Optional[CouncilTax] = None
    priceHistory: Optional[List[PriceEvent]] = None
    daysOnMarket: Optional[int] = None
    chain: Optional[Chain] = None
    soldComps: Optional[List[Ref]] = None
    newBuild: Optional[bool] = None


@dataclass
class RentDetails:
    """The rent surface (05-modes §5.3); present when Listing.mode == rent. No
    tenure/lease (short-horizon); the value slot reads asking-rent comps, not
    Land Registry."""
    rentPcm: Money = None
    deposit: Optional[Money] = None
    letType: Optional[str] = None          # long_term | short_term | student
    availableFrom: Optional[str] = None
    furnished: Optional[str] = None        # furnished | unfurnished | part
    billsIncluded: Optional[bool] = None
    epc: Optional[Epc] = None
    councilTax: Optional[CouncilTax] = None


@dataclass
class InvestDetails:
    """The invest surface (03-engine §5.4); present when Listing.mode == invest. The
    Financial scorer fills the value slot from these (yield/cashflow) instead of the
    Value Verdict — the fourth product's own value source."""
    estRentPcm: Money = None
    price: Money = None
    dealFlags: Optional[List[str]] = None
    estRefurbCost: Optional[Money] = None
    tenure: Optional[str] = None
    grossYieldAdvertised: Optional[float] = None   # the pool's stated yield, for cross-check


@dataclass
class InvestFinancials:
    """The Financial scorer's computed numbers (03-engine §5.4) — the invest analogue
    of the Value Verdict's evidence. Financing is not modelled (OQ 8.6)."""
    grossYieldPct: float = None
    annualRent: int = None
    annualCosts: int = None
    netYieldPct: float = None
    monthlyCashflow: int = None
    voidWeeks: float = None
    mgmtPct: float = None
    maintenancePct: float = None
    medianYieldPct: Optional[float] = None
    cohortSize: Optional[int] = None


@dataclass
class FloodRisk:
    band: str = None
    source: str = None                 # "environment_agency"


@dataclass
class Derived:
    forensics: Optional[Ref] = None
    pricePerSqft: Optional[float] = None
    floodRisk: Optional[FloodRisk] = None


@dataclass
class Listing:
    id: str = None
    schemaVersion: str = "listing@1"
    listingKey: str = None
    portalIds: List[PortalId] = None
    mode: Mode = None                  # buy | rent for a Listing
    address: Address = None
    geo: GeoPoint = None
    propertyType: PropertyType = None
    beds: int = None
    baths: int = None
    description: str = None
    images: List[Media] = None
    agent: Agent = None
    provenance: Provenance = None
    receptions: Optional[int] = None
    sqft: Optional[int] = None
    sqm: Optional[float] = None
    keyFeatures: Optional[List[str]] = None
    floorplans: Optional[List[Media]] = None
    nearestStations: Optional[List[Station]] = None
    buy: Optional[BuyDetails] = None
    rent: Optional[RentDetails] = None
    invest: Optional[InvestDetails] = None
    derived: Optional[Derived] = None
    raw: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Comp — comp@1 (01-domain §5.4a) — a sold comparable (HM Land Registry PPD).
# Added by U9 (the Land Registry Price Paid adapter). schemas.py had no Comp
# before U9 — the golden fixture only linked comps as bare Ref(schemaVersion=
# "comp@1"). This is the minimal, PPD-populatable record; `geo`, numeric
# `distanceMiles`, `sqft` and a Money-wrapped price are the richer canonical
# comp@1 fields P9/P3 add on top (reconciliation table on ``Comp`` below).
# ---------------------------------------------------------------------------

@dataclass
class CompAddress:
    """The Land Registry Price Paid address parts (paon / saon / street / postcode).

    Reconciliation: canonical comp@1 (§5.4a) carries a single display ``address``
    string plus ``postcode``; U9 keeps the structured PPD parts and derives the
    display string via :meth:`display`. ``saon`` (flat / sub-building name) is
    frequently absent on a Price Paid record.
    """
    paon: Optional[str] = None       # Primary Addressable Object Name (house number)
    saon: Optional[str] = None       # Secondary AON (flat / unit) — often absent
    street: str = None
    postcode: Optional[str] = None
    district: Optional[str] = None
    town: Optional[str] = None
    county: Optional[str] = None

    def display(self) -> str:
        """A human comp address, e.g. ``"Flat 2, 14 Northchurch Road, N1 4EG"``."""
        head = " ".join(b for b in (self.paon, self.street) if b)
        if self.saon and head:
            head = "%s, %s" % (self.saon, head)
        elif self.saon:
            head = self.saon
        return "%s, %s" % (head, self.postcode) if (head and self.postcode) else (head or self.postcode or "")


@dataclass
class Comp:
    """A sold comparable from HM Land Registry Price Paid (glossary: Comp).

    Field reconciliation with the canonical comp@1 (01-domain §5.4a:
    ``{id, schemaVersion, address, postcode, geo, soldPrice:Money, soldDate,
    propertyType, tenure, sqft?, pricePerSqft?, distanceMiles, source,
    sourceDate}``):

      ==============  ======================  ==================================
      U9 field        canonical comp@1        note
      ==============  ======================  ==================================
      price:int       soldPrice.amount        U9 carries the bare int; the Money
                                              wrapper is applied when a Comp is
                                              promoted into ``buy.soldComps``.
      date            soldDate                ISO YYYY-MM-DD (PPD gives the RFC-822
                                              "Tue, 28 Apr 2026" — parsed here).
      address         address + postcode      U9 keeps the structured PPD parts
                                              (:class:`CompAddress`).
      propertyType    propertyType            PPD slug: flat-maisonette |
                                              terraced | semi-detached |
                                              detached | other.
      tenure          tenure                  PPD estateType: Freehold | Leasehold.
      newBuild        (not in comp@1)         PPD extra, kept for context.
      pricePerSqft    pricePerSqft            ALWAYS None from PPD — Land Registry
                                              has no floor area; £/sqft needs the
                                              EPC register (a free key, DEFERRED).
      distanceNote    distanceMiles           a street query returns no per-comp
                                              geo, so U9 records a human note
                                              ("same street" / street name) rather
                                              than a numeric mile distance.
      ==============  ======================  ==================================

    ``geo``, ``sqft`` and a numeric ``distanceMiles`` are enrichment P9/P3 add
    later; U9 leaves them off the minimal record.
    """
    price: int = None
    date: str = None                       # ISO YYYY-MM-DD (parsed from PPD transactionDate)
    address: CompAddress = None
    propertyType: str = None               # PPD slug, e.g. "flat-maisonette"
    tenure: str = None                     # "Freehold" | "Leasehold"
    newBuild: bool = None
    distanceNote: str = None               # "same street" | "De Beauvoir Road" | ...
    pricePerSqft: Optional[float] = None   # None from PPD; filled by epc.enrich_comps (price/sqft)
    sqft: Optional[float] = None           # EPC total_floor_area, m²→sqft (×10.7639); provenance for pricePerSqft
    epcCertNumber: Optional[str] = None    # EPC certificate the floor area came from — provenance
    # EPC data-integrity provenance (epc.enrich_comps; None where unmatched) —
    # the floor area is chosen as configured *at the comp's sale date*, not the
    # newest EPC, so a 2026 sale is not divided by a 2012 area.
    epcDate: Optional[str] = None          # registrationDate of the sale-matched EPC (ISO)
    epcSaleGapYears: Optional[float] = None # |sale date − epcDate| in years (staleness)
    areaChanged: Optional[bool] = None     # ≥2 EPCs at the address differ >5% or >5 m² (extension/re-measure)
    epcAfterSaleOnly: Optional[bool] = None # no EPC pre-dated the sale; a later one was used (low trust)
    areaConfidence: Optional[str] = None   # "high" | "medium" | "low" — trust in the £/sqft
    epcAreaChange: Optional[Dict[str, Any]] = None  # {minM2,maxM2,minDate,maxDate,epcCount} when areaChanged
    schemaVersion: str = "comp@1"
    source: str = "hm_land_registry"       # canonical comp@1 source spelling (§5.4a)
    sourceDate: Optional[str] = None       # PPD fetch / release date (ISO)
    transactionId: Optional[str] = None    # PPD transactionId — the dedupe key


# ---------------------------------------------------------------------------
# Scoring interface — score.request@1 -> score.result@1 (01-domain §5.5).
# ---------------------------------------------------------------------------

@dataclass
class ScoreRequestOptions:
    useImages: Optional[bool] = None
    useFloorplans: Optional[bool] = None
    explain: Optional[str] = None


@dataclass
class ScoreRequest:
    """score.request@1 — also reused as the `request` echo on a ScoreResult."""
    schemaVersion: str = "score.request@1"
    listingRef: Ref = None
    personRef: Ref = None
    searchRef: Ref = None
    personVersionSnapshot: int = None
    searchConfigHash: str = None
    options: Optional[ScoreRequestOptions] = None


@dataclass
class AxisBreakdown:
    """One row of taste.axisBreakdown; all eight are required (§5.7 rule 2)."""
    axis: TasteAxis = None
    score: float = None
    weight: float = None
    contribution: str = None


@dataclass
class TasteAdjustment:
    """A signed, sourced correction between the weighted base and taste.score."""
    kind: str = None       # named_love | anti_signal | learned_rule ...
    delta: float = None
    source: str = None


@dataclass
class TasteResult:
    score: float = None
    prior: float = None    # text-only pass (round2 text_prior)
    staged: bool = None
    axisBreakdown: List[AxisBreakdown] = None
    tasteAdjustments: List[TasteAdjustment] = None


@dataclass
class ValueBand:
    low: int = None
    high: int = None


@dataclass
class ValueEvidence:
    kind: str = None       # comp | ppsf | reduction | dom | lease_adj
    label: str = None
    value: float = None    # bare number (£/sqft, £ delta, days, ...)
    compRef: Optional[Ref] = None
    text: Optional[str] = None


@dataclass
class ValueVerdict:
    """value_verdict@1 — the score.result@1.valueVerdict sub-object (§5.5b),
    carrying the four gauge fields P3 §5.2 adds (fairEstimate, band, position,
    streetMedianPerSqft). THE Buy differentiator."""
    score: float = None            # the 0-10 value component the Mix weights
    tag: ValueTag = None
    deltaPct: float = None         # the adjusted (honest) figure
    headlineDeltaPct: float = None  # the naive figure
    fairEstimate: int = None       # bare integer pounds
    band: ValueBand = None
    position: float = None         # in [0,1], for the P2 gauge
    streetMedianPerSqft: Optional[float] = None   # buy always sets it; rent may lack sqft
    basis: str = None
    evidence: List[ValueEvidence] = None
    confidence: float = None       # per-scorer Confidence scalar in [0,1]


@dataclass
class GateResult:
    code: str = None
    passed: bool = None            # domain field `pass` (a Python keyword)
    soft: Optional[bool] = None


@dataclass
class SoftDock:
    rule: str = None
    delta: float = None


@dataclass
class RulesResult:
    score: float = None
    gatesPassed: bool = None
    excluded: bool = None          # hard-gate exclusion marker (§5.0 step 2)
    gateResults: List[GateResult] = None
    softDocks: Optional[List[SoftDock]] = None


@dataclass
class Reason:
    scorer: str = None             # taste | value | rules
    polarity: str = None           # "+" | "−"
    text: str = None
    evidenceRefs: Optional[List[str]] = None  # intra-object keys into evidence[]


@dataclass
class Forensics:
    """forensics@1 (03-engine §5.5) — the photo + floorplan vision read, computed
    ONCE per ``listingKey`` and Person-independent (the expensive vision compute the
    unit-economics contract runs once; every Person's taste/value read consumes it
    cheaply). P9 owns fetch/storage/cache-invalidation (a new ``imageSetHash`` on a
    relist); this spec owns *what the model extracts and the flags it feeds*."""
    id: str = None
    schemaVersion: str = "forensics@1"
    listingKey: str = None                        # the cache key (once per listingKey)
    roomWidthsM: Optional[List[float]] = None     # floorplan scale-bar widths (skinny rooms text hides)
    walkThroughBedroom: Optional[bool] = None     # a bedroom crossed to reach another
    hmoTells: Optional[bool] = None               # multiple ensuites / second kitchen / bedroom locks
    cheapFlipSignals: Optional[List[str]] = None  # grey landlord refurb, laminate, white-box flip (FATAL kill)
    aspect: Optional[str] = None                  # window orientation ("south-west (rear)", "north-facing")
    ceilingHeightCue: Optional[str] = None         # door:ceiling ratio read ("generous (bay)", "low")
    floorPosition: Optional[str] = None           # floorplan floor read ("raised + lower ground")
    imageSetHash: Optional[str] = None            # P9 cache-invalidation key (relist)


@dataclass
class Flag:
    code: FlagCode = None
    severity: FlagSeverity = None
    text: str = None
    kind: FlagKind = None
    source: str = None             # a Listing field path or "forensics"


@dataclass
class ConfidenceReport:
    """Bundles the three per-scorer Confidence scalars; overall recomputes."""
    overall: float = None
    taste: float = None
    value: float = None
    rules: float = None
    drivers: Optional[List[str]] = None
    missing: Optional[List[str]] = None


@dataclass
class ComponentSource:
    """Provenance-lite label on a SourcedComponent."""
    label: str = None
    freshness: Optional[str] = None


@dataclass
class SourcedComponent:
    """A score.result@1 components[] handle: what the engine can feed + sources."""
    component: ComponentName = None
    availability: Availability = None
    sources: List[ComponentSource] = None


@dataclass
class ScoreResult:
    """score.result@1 — the contract everything downstream renders (§5.5b).

    On a hard-gate exclusion (rules.excluded True), composite is a forced 0 and
    both taste and valueVerdict are None (§5.0 step 2) — hence they are Optional.
    """
    id: str = None
    schemaVersion: str = "score.result@1"
    request: ScoreRequest = None
    composite: float = None
    rules: RulesResult = None
    reasons: List[Reason] = None
    flags: List[Flag] = None
    confidence: ConfidenceReport = None
    components: List[SourcedComponent] = None
    scoredAt: str = None
    taste: Optional[TasteResult] = None         # null on exclusion
    valueVerdict: Optional[ValueVerdict] = None  # null on exclusion


# ---------------------------------------------------------------------------
# Component library — component.spec@1 (06-dashboards §5.0).
# ---------------------------------------------------------------------------

@dataclass
class ComponentInput:
    from_: str = None      # domain field `from` (a Python keyword)
    path: str = None
    required: bool = None


@dataclass
class WhenShown:
    predicate: str = None
    prose: str = None


@dataclass
class ComponentSpec:
    """component.spec@1 — one Component's definition record in the P6 library."""
    schemaVersion: str = "component.spec@1"
    component: ComponentName = None
    tier: int = None                 # 1 | 2 | 3
    typicalZone: Zone = None
    primitive: str = None
    inputs: List[ComponentInput] = None
    whenShown: WhenShown = None
    sources: List[str] = None
    sophistication: Dict[str, str] = None   # {plain, warm, forensic}
    emptyState: str = None
    modes: Dict[str, str] = None            # {buy, rent, invest, dream}
    answers: Optional[List[str]] = None
    crossListing: Optional[bool] = None


# ---------------------------------------------------------------------------
# Generative dashboard — the P6 assembler contracts (06-dashboards §5.0-§5.4).
# selectComponents() -> Slot[]; assembleDashboard() -> dashboard.layout@1.
# ---------------------------------------------------------------------------

class Stage(str, Enum):
    """pursuit@1 Buy Lifecycle stage — gates progressive disclosure (§5.3)."""
    BROWSE = "browse"
    SHORTLIST = "shortlist"
    VIEWING = "viewing"
    OFFER = "offer"


class SlotForm(str, Enum):
    """A lead-zone tier-3's render size (§5.3 step 6): compact at browse, full for finalists."""
    COMPACT = "compact"
    FULL = "full"


class SlotState(str, Enum):
    """A Slot's data readiness (rule 4): ready renders, needs_data shows emptyState."""
    READY = "ready"
    NEEDS_DATA = "needs_data"


class Expansion(str, Enum):
    """Sophistication-stamped default depth (§5.3 step 8 / §5.5)."""
    COLLAPSED = "collapsed"              # headline only
    COLLAPSED_PLUS = "collapsed-plus"    # headline + one-tap reveal (plain default)
    EXPANDED = "expanded"                # open inline (forensic default for tier-3)


@dataclass
class DashboardBlock:
    """A mode.profile@1.dashboard block (§5.4): the per-Mode Component layout the
    selector seeds from. Zone arrays are ORDER-BEARING — `profileIndex` (a
    Component's position in its zone array) is the §5.3 step-7 tie-break."""
    lead: List[str] = None
    body: List[str] = None
    finalist: List[str] = None
    suppressed: Optional[List[str]] = None
    stageEmphasis: Optional[Dict[str, List[str]]] = None   # stage -> [components] promoted to lead front
    stageScope: Optional[Dict[str, List[str]]] = None      # component -> [stages] it is allowed in


@dataclass
class ComponentLibrary:
    """componentLibrary@1 — the single registry of component.spec@1 records (§5.0)."""
    schemaVersion: str = "componentLibrary@1"
    libraryVersion: str = None       # semver; bumps on add/retier
    components: List[ComponentSpec] = None


@dataclass
class Slot:
    """One selected Component in the resolved dashboard (§5.3 output row). The
    additive `expansion` is the P6 extension of P5's Slot (§9 REQUIRED P5 CHANGE)."""
    component: ComponentName = None
    tier: int = None
    zone: str = None                 # lead | body | finalist
    form: str = None                 # compact | full
    state: str = None                # ready | needs_data
    expansion: str = None            # collapsed | collapsed-plus | expanded
    sources: Optional[List[str]] = None
    reason: str = None               # which rule/step placed it (explainability)


@dataclass
class Narration:
    """The one AI-narrated pair fronting the dashboard (rule 5). Bounded to fields
    already present in the sourced result — cites, never editorialises."""
    headline: str = None
    subhead: str = None


@dataclass
class DashboardLayout:
    """dashboard.layout@1 — the assembleDashboard() output: the ordered Slot[] plus
    the single narrated pair. Reproducible against {libraryVersion,
    engineConfigVersion, profileVersion, searchConfigHash, pursuit.stage} (§5.0)."""
    schemaVersion: str = "dashboard.layout@1"
    mode: Mode = None
    stage: Stage = None
    sophistication: str = None       # plain | warm | forensic
    slots: List[Slot] = None
    narration: Narration = None
    libraryVersion: Optional[str] = None


# ---------------------------------------------------------------------------
# M3 — the slice experience: the cross-listing feed (05-modes assembleFeed) and
# the ingestion envelope (09-data-trust §5.1b).
# ---------------------------------------------------------------------------

@dataclass
class FeedCard:
    """One listing's row in the Buy shortlist feed — its browse-stage lead render
    (the calm feed card, §5.3): the compact Value Verdict + top flag + the score
    rings, plus the sort key. The heavy Components stay behind the drill-down."""
    listingRef: Ref = None
    addressDisplay: str = None
    price: Optional[Money] = None
    facts: Optional[str] = None            # "Maisonette · 2 bed · 2 bath · 1050 sqft"
    composite: float = None                # the feed sort key
    taste: Optional[float] = None
    verdictTag: Optional[ValueTag] = None
    deltaPct: Optional[float] = None
    headlineDeltaPct: Optional[float] = None
    topFlag: Optional[Flag] = None
    slots: Optional[List[Slot]] = None     # the browse Slot[] (value_verdict compact, risk_flags)
    isDemo: Optional[bool] = None


@dataclass
class FeedLayout:
    """feed.layout@1 (05-modes) — the cross-listing surface: the Buy shortlist as a
    ranked set of :class:`FeedCard`, fronted by one narrated line over the set. The
    invest ``deal_table`` compare variant is contract-level (deferred)."""
    schemaVersion: str = "feed.layout@1"
    searchRef: Ref = None
    mode: Mode = None
    stage: Stage = None
    cards: List[FeedCard] = None
    narration: Optional[Narration] = None
    sources: Optional[List[str]] = None
    assembledAt: Optional[str] = None


class IngestChannel(str, Enum):
    """How a Listing entered (09-data-trust §5.1): a forwarded portal alert or a paste."""
    FORWARDED_ALERT_EMAIL = "forwarded_alert_email"
    PASTE_LINK = "paste_link"


class IngestState(str, Enum):
    """ingest.event@1 state machine (§5.1b): received → … → scored → done (or failed)."""
    RECEIVED = "received"
    PARSED = "parsed"
    NORMALISED = "normalised"
    DEDUPED = "deduped"
    ENRICHED = "enriched"
    SCORED = "scored"
    DONE = "done"
    QUARANTINED = "quarantined"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class IngestEvent:
    """ingest.event@1 (§5.1b) — the envelope wrapping one inbound artefact (a
    forwarded email or a pasted link) from receipt to normalised Listing."""
    schemaVersion: str = "ingest.event@1"
    id: str = None
    channel: IngestChannel = None
    receivedAt: Optional[str] = None
    dedupeKey: str = None                  # sha1(channel + rawBody) — re-forward = no-op
    state: IngestState = None
    disposition: Optional[str] = None      # new_listing | duplicate_ignored | merged_into_existing | parse_failed
    listingRef: Optional[Ref] = None
    listingKey: Optional[str] = None


# ---------------------------------------------------------------------------
# P4 · Elicitation & taste-learning (04-elicitation.md §5). Every object here is
# THIS phase's own, keyed by personRef+profileVersion — NO new person@1 /
# feedback@1 field is added (A11); the mutation writes existing Person fields and
# these side objects. FeedbackKind / AntiSignal / Ref / Mode already exist above.
# ---------------------------------------------------------------------------

class ProbeKind(str, Enum):
    """probe@1.kind (§5.3). `swipe_card` is scored at the `swipe_bare` noise floor
    and sharpens to with-why/voice at apply time (§5.0)."""
    SWIPE_CARD = "swipe_card"
    THIS_OR_THAT = "this_or_that"
    VOICE_RATE = "voice_rate"
    FORCED_RANK = "forced_rank"
    CALIBRATION_HOME = "calibration_home"
    ONE_WORD = "one_word"


class SwipeGesture(str, Enum):
    """The four Swipe-deck gestures (§5.4); each maps to a defined mutation."""
    RIGHT = "right"     # like
    LEFT = "left"       # dislike
    UP = "up"           # love-it-wrong-time → Dream (taste-only)
    TAP = "tap"         # why → sharpens the read


@dataclass
class AxisBelief:
    """taste.uncertainty@1.axes[axis] — belief about one taste axis."""
    mean: float = None            # 0-10 weight-informed belief
    sigma: float = None           # the uncertainty (σ0=3.0 cold → σMin=0.7 learned)
    nObs: int = None


@dataclass
class AntiSignalBelief:
    """taste.uncertainty@1.antiSignals[signal] — a candidate/confirmed dislike."""
    leaning: str = None           # like | dislike | unknown
    strength: float = None
    mentions: int = None
    confirmed: bool = None        # two mentions make it stick (§5.5)
    sigma: float = None


@dataclass
class UncertaintyOverall:
    clarity0to1: float = None     # the meter value (weight-aware, §5.2)
    sigmaMean: float = None
    weakestAxes: List[str] = None # what to ask about next


@dataclass
class UncertaintyProvenance:
    seededFromTwin: bool = None
    lastProbeAt: Optional[str] = None
    twinRef: Optional[Ref] = None


@dataclass
class TasteUncertainty:
    """taste.uncertainty@1 (§5.2) — the engine's honest account of what it does
    NOT yet know about a Person. A separate object (keyed by personRef +
    profileVersion), so no new person@1 field is needed."""
    schemaVersion: str = "taste.uncertainty@1"
    personRef: Ref = None
    profileVersion: int = None
    axes: Dict[str, AxisBelief] = None
    antiSignals: Dict[str, AntiSignalBelief] = None
    archetypeCoverage: Dict[str, int] = None
    provenance: UncertaintyProvenance = None
    overall: UncertaintyOverall = None
    areaAffinity: Optional[Dict[str, Any]] = None


@dataclass
class Probe:
    """probe@1 (§5.3) — one candidate ask; `expectedInfoGain` is the selection key
    and `valuePayload` enforces value-before-ask."""
    schemaVersion: str = "probe@1"
    id: str = None
    kind: ProbeKind = None
    informs: List[Dict[str, Any]] = None     # [{target:"axis"|"antiSignal", key}]
    valuePayload: Dict[str, Any] = None      # a matched home + why (value, not a bare ask)
    expectedInfoGain: float = None
    listingRefs: Optional[List[Ref]] = None
    prompt: Optional[str] = None


@dataclass
class SwipeCard:
    """swipe.card@1 (§5.4) — a real Listing as a swipeable card. The gesture is the
    ask; the card and its tap-reveal are the value. Score ring withheld first."""
    schemaVersion: str = "swipe.card@1"
    listingRef: Ref = None
    caption: str = None                      # one factual line, no adjectives
    priceShown: bool = None
    scoreHidden: bool = None
    whyOnTap: Dict[str, Any] = None          # {valueVerdictRef?, tasteReasons[]}
    probeRef: Ref = None
    hero: Optional[str] = None               # image label (URL-free embed)


@dataclass
class TastePrior:
    mean: float = None
    sigma: float = None                      # ≥ priorSigmaFloor (2.6) — loosely held


@dataclass
class TwinPrivacy:
    kAnonymised: bool = None
    dpNoiseSigma: float = None
    noIndividualData: bool = None
    builtAt: Optional[str] = None


@dataclass
class TasteTwin:
    """taste.twin@1 (§5.9) — the privacy-safe cohort cold-start prior (n≥50, DP
    noise, volunteered cohortKey only); calibrates away fast."""
    schemaVersion: str = "taste.twin@1"
    id: str = None                           # a COHORT id, never a person id
    cohortKey: Dict[str, Any] = None         # {lifeStage, city, seedLoves[]}
    n: int = None
    priors: Dict[str, TastePrior] = None
    privacy: TwinPrivacy = None
    antiSignalPriors: Optional[Dict[str, Any]] = None


@dataclass
class RewardState:
    """reward.state@1 (§5.10) — meter + streak + discovery, all driven by the same
    uncertainty model so they can never lie about progress."""
    schemaVersion: str = "reward.state@1"
    personRef: Ref = None
    clarity: float = None
    clarityDelta: float = None
    milestonesHit: List[float] = None
    streak: Optional[Dict[str, Any]] = None
    discoveries: Optional[List[Dict[str, Any]]] = None


@dataclass
class ElicitationSession:
    """elicitation.session@1 (§5.0) — the loop's resumable record; the container
    P5/P6 read. A pause loses nothing."""
    schemaVersion: str = "elicitation.session@1"
    id: str = None
    personRef: Ref = None
    mode: Mode = None
    probesServed: List[Ref] = None
    feedbackRefs: List[Ref] = None
    uncertaintyBefore: Ref = None
    uncertaintyAfter: Ref = None
    reward: Dict[str, Any] = None
    status: str = None                       # active | paused | complete
    startedAt: Optional[str] = None
    lastActiveAt: Optional[str] = None


# ---------------------------------------------------------------------------
# P7 · Shell & homepage (07-shell.md §5). Render/route contracts only — no
# scoring, no Person mutation, no new P1 shape (bar the deferred search.status).
# ---------------------------------------------------------------------------

@dataclass
class Route:
    """route@1 (§5.1) — a parsed URL; resolveRoute guarantees every hash resolves
    to a live view (the mode's home at worst), never a blank."""
    schemaVersion: str = "route@1"
    view: str = None
    raw: str = None
    arg: Optional[str] = None


@dataclass
class NavModel:
    """nav.model@1 (§5.1) — the per-Mode view set the nav renders."""
    schemaVersion: str = "nav.model@1"
    mode: Mode = None
    home: str = None
    primary: List[str] = None
    secondary: List[str] = None
    overlays: List[str] = None


@dataclass
class FrontDoor:
    """homepage.spec@1.frontDoors[] — one honest door per Mode."""
    mode: Mode = None
    label: str = None
    promise: str = None
    leadWith: str = None
    cta: str = None


@dataclass
class HomepageSpec:
    """homepage.spec@1 (§5.2) — the pre-signup front door: it performs the product
    (swipe real homes → an instant taste-read) rather than explaining it."""
    schemaVersion: str = "homepage.spec@1"
    headline: Dict[str, Any] = None          # {equation, sub}
    demo: Dict[str, Any] = None              # the live on-page swipe-demo config
    tasteRead: Dict[str, Any] = None         # the instant reveal after N swipes
    frontDoors: List[FrontDoor] = None
    handoff: Dict[str, Any] = None
    provenance: Provenance = None


@dataclass
class SwitcherEntry:
    """search.switcher@1.searches[] (§5.3) — one Search as the switcher sees it.
    All fields project from an existing search@1 (title/mode/scorerMix/provenance/
    status/collaborators); the shell reads, never redefines (A14)."""
    searchRef: Ref = None
    title: str = None
    mode: Mode = None
    mixSummary: str = None                    # "55/20/25" (taste/rules/value)
    badge: str = None                         # LIVE | DEMO PERSONA | DRAFT | PAUSED | ARCHIVED
    role: str = None                          # owner | editor | viewer (P1 role)
    subtitle: Optional[str] = None
    provenance: Optional[Provenance] = None


@dataclass
class Switcher:
    """search.switcher@1 (§5.3) — one Person, many Searches. Proves the
    Person-vs-Search architecture is navigable: one personRef, N view sets, no
    dead end (canCreate always true)."""
    schemaVersion: str = "search.switcher@1"
    personRef: Ref = None
    activeSearchRef: Ref = None
    searches: List[SwitcherEntry] = None
    canCreate: bool = True


@dataclass
class ShellLayout:
    """shell.layout@1 (§5.0) — the persistent frame's resolved state. `anonymous`
    is the discriminant: on the homepage there is no active Search yet, so
    `activeSearchRef` and `mode` are absent and `route` is the mode-less homepage
    view (§5.2). It renders and routes; it computes no score (A14)."""
    schemaVersion: str = "shell.layout@1"
    route: Route = None
    viewport: str = None                      # rail (>=861) | compact (<=860)
    theme: str = None                         # system | light | dark
    anonymous: bool = None
    resolvedAt: str = None
    activeSearchRef: Optional[Ref] = None      # absent when anonymous
    mode: Optional[Mode] = None                # absent when anonymous
    overlay: Optional[str] = None              # none | switch | settings | fork | confirm


# ---------------------------------------------------------------------------
# P7 · Editing a Search (07-shell.md §5.4/§5.5) — the plain-language edit loop and
# the sub-search fork. Both mutate ONLY search@1; the Person is edited solely
# through the P4 teach path (A11). Delta/Subsearch are P1-owned shapes (subsearch@1,
# 01-domain §5.3) the fork consumes; placed with the fork that first needs them.
# (`from` is a Python keyword → `from_`; to_jsonable emits "from".)
# ---------------------------------------------------------------------------

class DeltaDirection(str, Enum):
    WIDEN = "widen"
    NARROW = "narrow"
    MOVE = "move"


@dataclass
class Delta:
    """subsearch@1.delta[] (01-domain §5.3) — a raw parameter change, numeric from/to."""
    path: str = None
    from_: Any = None
    to: Any = None
    direction: DeltaDirection = None


@dataclass
class Subsearch:
    """subsearch@1 (01-domain §5.3) — a fork: the Person + parent Scorer Mix held
    fixed, some parameters overridden, the computed delta stored."""
    schemaVersion: str = "subsearch@1"
    id: str = None
    parentRef: Ref = None
    personRef: Ref = None
    inherits: Dict[str, Any] = None            # {scorerMix:true, gates:true, area:false, alertPolicy:true}
    overrides: Dict[str, Any] = None
    delta: List[Delta] = None
    status: SearchStatus = None
    createdAt: Optional[str] = None


@dataclass
class RenderedDelta:
    """subsearch.forkview@1.delta[] (§5.4) — the display projection of a P1 Delta:
    path + direction raw, from/to formatted to house strings ("£1,500,000")."""
    path: str = None
    from_: str = None
    to: str = None
    direction: DeltaDirection = None


@dataclass
class ForkView:
    """subsearch.forkview@1 (§5.4) — the fork flow's state. A fork is never left in
    limbo: `resolution` is always reachable (Promote → sibling Search, or Discard →
    parent untouched, nothing logged). The Scorer Mix cannot be forked (that is a
    new Search, P1 rule); the fork holds it fixed."""
    schemaVersion: str = "subsearch.forkview@1"
    parentRef: Ref = None
    draft: Subsearch = None
    delta: List[RenderedDelta] = None
    preview: Dict[str, Any] = None             # {parentCount, subCount, movers{entered,left,reranked}}
    resolution: str = None                     # open | promoted | discarded


class EditEffect(str, Enum):
    REAL = "real"
    NOISE = "noise"


@dataclass
class EditChange:
    """nl.edit@1.EditChange (§5.5) — one parsed change. `effect:noise` = a clause
    that matched no editable field (surfaced honestly, never silently dropped)."""
    kind: str = None                           # threshold|budget|beds|baths|sqft|addarea|droparea|lease|tenure|noise
    field: str = None                          # the search@1 path it writes ("" for noise)
    plain: str = None                          # "Max budget £1,350,000 -> £1,500,000"
    effect: EditEffect = None
    from_: Optional[Any] = None
    to: Optional[Any] = None
    label: Optional[str] = None
    reason: Optional[str] = None               # why a no-op/noise clause did nothing


@dataclass
class EditDiff:
    """nl.edit@1.EditDiff (§5.5) — the parsed instruction, PREVIEWED not applied.
    Nothing mutates the Search until the user confirms (A8). `realCount==0` disables
    Apply; `hasNoise` surfaces the honest 'not understood yet' line (A9)."""
    changes: List[EditChange] = None
    realCount: int = None
    hasNoise: bool = None


@dataclass
class ReRankReceipt:
    """nl.edit@1.ReRankReceipt (§5.5) — the felt result of Apply: how the feed
    moved. The receipt is the shell's; the re-ranked scores are P3's."""
    beforeCount: int = None
    afterCount: int = None
    appliedCount: int = None
    delta: Optional[int] = None
    movers: Optional[Dict[str, Any]] = None    # {entered, left, reranked}


@dataclass
class ChangeEntry:
    """changelog@1.entries[] (§5.5) — one applied change, in plain words. Append-
    only: a revert is a new entry, never a deletion (the history stays honest)."""
    n: int = None
    at: str = None
    plain: str = None
    source: str = None                         # nl | control
    byRole: Optional[str] = None


@dataclass
class Changelog:
    """changelog@1 (§5.5) — the append-only, per-Search, plain-words record. 'The
    changelog is the trust.'"""
    schemaVersion: str = "changelog@1"
    searchRef: Ref = None
    entries: List[ChangeEntry] = None


@dataclass
class SettingsPanel:
    """settings.panel@1 (§5.5) — the per-Search editing surface. Writes ONLY
    search@1 (never the Person, A11). `editable:false` for a viewer collaborator:
    read-only, still fully readable (no dead end)."""
    schemaVersion: str = "settings.panel@1"
    searchRef: Ref = None
    editable: bool = None
    controls: Dict[str, Any] = None            # gates/threshold/budget/area/scorerMix (direct controls)
    changelog: Changelog = None
    pendingDiff: Optional[EditDiff] = None


# ---------------------------------------------------------------------------
# P8 · Action layer — alerts (08-action.md §5.4). The delivery engine over P1's
# per-Search alertPolicy + threshold: it decides whether a scored listing alerts,
# when, how, and bundled how — the only-surface-7-plus noise gate made felt. Own
# objects, keyed by refs; no new P1 field (A13).
# ---------------------------------------------------------------------------

class AlertType(str, Enum):
    """alert.event@1.type (§5.4). `verdict_change` is the PROTECTIVE alert."""
    NEW_MATCH = "new_match"
    PRICE_DROP = "price_drop"
    BACK_ON_MARKET = "back_on_market"
    VERDICT_CHANGE = "verdict_change"        # protective: saved-from-a-mistake
    STATUS_CHANGE = "status_change"


class AlertState(str, Enum):
    PENDING = "pending"
    BATCHED = "batched"
    SENT = "sent"
    SUPPRESSED = "suppressed"


@dataclass
class AlertGate:
    """alert.event@1.gate — why it did/didn't clear the only-surface-7-plus gate."""
    minComposite: float = None
    passed: bool = None
    protectiveBypass: bool = None


@dataclass
class AlertEvent:
    """alert.event@1 (§5.4) — one evaluated trigger. `state:pending` clears the
    gate and enters a digest; `suppressed` carries the honest `suppressReason`."""
    schemaVersion: str = "alert.event@1"
    id: str = None
    searchRef: Ref = None
    listingRef: Ref = None
    scoreResultRef: Ref = None
    type: AlertType = None
    trigger: Dict[str, Any] = None           # {was, now, delta}
    composite: float = None
    gate: AlertGate = None
    channel: str = None                      # email | in_app | digest
    state: AlertState = None
    pursuitRef: Optional[Ref] = None
    suppressReason: Optional[str] = None      # below_min_composite | dedupe | excluded | quiet_hours_held
    createdAt: Optional[str] = None
    deliveredAt: Optional[str] = None


@dataclass
class Digest:
    """digest@1 (§5.4) — a batched delivery: the window's pending events ranked by
    composite desc (protective to top), capped at maxPerDigest with the overflow
    rolledOver (never dropped). `narration` cites the items, never invents."""
    schemaVersion: str = "digest@1"
    id: str = None
    searchRef: Ref = None
    personRef: Ref = None
    cadence: str = None                      # instant | daily | weekly
    window: Dict[str, Any] = None            # {from, to}
    channel: str = None
    items: List[Ref] = None                  # → alert.event@1
    count: int = None
    capped: bool = None
    rolledOver: List[Ref] = None             # → alert.event@1 (deferred to next digest)
    narration: Dict[str, Any] = None         # {headline, subhead}
    sentAt: Optional[str] = None


# ---------------------------------------------------------------------------
# P8 · Viewing mode — viewing.record@1 (08-action.md §5.1). The retention hinge:
# the score's kind:"viewing" flags become a tickable checklist, and a voice
# debrief re-weights the portable Person — writing it ONLY through P4's
# applyFeedback (A11), never a Person field here. Keyed by the pursuit@1 it
# belongs to (a Ref; no Pursuit object needed). (`class` is a keyword → class_;
# to_jsonable emits "class".)
# ---------------------------------------------------------------------------

@dataclass
class ChecklistItem:
    """viewing.record@1.checklist[] (§5.1a) — one in-person check. Every line is
    sourced: no line asserts a fact the engine did not surface (A1)."""
    id: str = None
    source: str = None                 # viewing_flag|buy_question|axis_verify|playbook|listing_flag|user
    kind: str = None                   # verify|measure|observe|ask_agent
    prompt: str = None
    priority: str = None               # must|should|nice
    status: str = "pending"            # pending|pass|fail|partial|skip
    mediaRefs: List[str] = None
    flagCode: Optional[str] = None
    question: Optional[str] = None
    axis: Optional[str] = None
    evidenceRef: Optional[str] = None  # intra-record pointer: flag:.. / forensics:.. / axis:..
    note: Optional[str] = None
    result: Optional[str] = None


@dataclass
class MediaCapture:
    """viewing.record@1.capturedMedia[] (§5.1) — user-captured, vault-stored, never
    published or sent to the agent (§5.3 privacy)."""
    localMediaId: str = None
    kind: str = None                   # photo|video
    capturedAt: str = None
    boundToItem: Optional[str] = None
    note: Optional[str] = None
    provenance: Optional[Dict[str, Any]] = None


@dataclass
class ItemResult:
    localItemId: str = None
    status: str = None                 # pass|fail|partial|skip
    result: Optional[str] = None


@dataclass
class OverallReaction:
    text: str = None
    ratingInferred: Optional[float] = None


@dataclass
class WouldChange:
    item: str = None
    class_: str = None                 # fixable | kill


@dataclass
class NewSignal:
    kind: str = None                   # anti_signal | named_love | area
    value: str = None


@dataclass
class DebriefExtracted:
    """Debrief.extracted (§5.1c) — the structured pull from the transcript (LLM
    extraction is the parked wire-up; the re-weight it feeds is real)."""
    itemResults: List[ItemResult] = None
    overallReaction: OverallReaction = None
    wouldChange: List[WouldChange] = None
    newSignals: List[NewSignal] = None


@dataclass
class VerdictAgreement:
    """The trust metric (00-frame): did the verdict hold once you stood in it?"""
    agreedAfterViewing: bool = None
    note: Optional[str] = None


@dataclass
class Debrief:
    """viewing.record@1.debrief (§5.1c) — the voice debrief. It (1) resolves the
    checklist, (2) records whether the verdict held, (3) re-weights the Person via
    P4 applyFeedback. The receipt is P4's — this only constructs the event (A11)."""
    extracted: DebriefExtracted = None
    verdictAgreement: VerdictAgreement = None
    transcript: Optional[str] = None   # raw voice→text provenance; the extracted drives the re-weight
    receipt: Dict[str, Any] = None     # the P4 applyFeedback receipt {before, after, summary, clarityDelta}
    feedbackRefs: List[Ref] = None
    recordingRef: Optional[Ref] = None
    debriefedAt: Optional[str] = None


@dataclass
class ViewingRecord:
    """viewing.record@1 (§5.1) — the per-property checklist + debrief. P8-owned,
    keyed by the pursuit@1 it belongs to; mutates neither Listing nor Person (its
    debrief writes the Person only through P4's applyFeedback)."""
    schemaVersion: str = "viewing.record@1"
    id: str = None
    pursuitRef: Ref = None
    searchRef: Ref = None
    listingRef: Ref = None
    personRef: Ref = None
    scoreResultRef: Ref = None
    checklist: List[ChecklistItem] = None
    status: str = None                 # prepared | in_progress | debriefed | archived
    createdAt: str = None
    updatedAt: str = None
    scheduledFor: Optional[str] = None
    capturedMedia: Optional[List[MediaCapture]] = None
    debrief: Optional[Debrief] = None
    verdictAgreement: Optional[VerdictAgreement] = None
    savedFromMistake: Optional[bool] = None


# ---------------------------------------------------------------------------
# P8 · Multiplayer — invite, fit-for-both, veto/voting (08-action.md §5.5). The
# the rent-with-a-housemate mission made real: a home is scored once per member's PORTABLE
# Person against the same Search config, and the results combine veto-aware. No
# new engine, no Person mutation (Person portability, P1 E / P5 A14).
# ---------------------------------------------------------------------------

@dataclass
class CollabInvite:
    """collab.invite@1 (§5.5a) — invite a housemate/partner into a Search under the
    P1 role enum. Sending is a permissioned user action; on accept the invitee's
    personRef is written into search.collaborators (P1). The invitee brings their
    OWN portable Person."""
    schemaVersion: str = "collab.invite@1"
    id: str = None
    searchRef: Ref = None
    invitedEmail: str = None
    invitedByPersonRef: Ref = None
    role: str = None                   # editor | viewer
    status: str = None                 # pending | accepted | declined | revoked
    createdAt: str = None
    personRef: Optional[Ref] = None    # written on accept
    message: Optional[str] = None
    respondedAt: Optional[str] = None


@dataclass
class MemberFit:
    """fit.combined@1.memberFits[] — one collaborator's own read (why THEY love it
    or don't), carrying their own score.result@1 ref (their portable Person)."""
    personRef: Ref = None
    scoreResultRef: Ref = None
    composite: float = None
    veto: bool = None
    topReasonForThem: Optional[str] = None
    topFlagForThem: Optional[str] = None


@dataclass
class CombinedFit:
    """fit.combined@1 (§5.5b) — fit-for-both. `combineFit` merges the members'
    score.result@1s by `harmonic_floor` (a home must work for EVERYONE, so a low
    outlier is punished, not averaged away); a veto forces the score to 0 and gates
    the home out of the shared shortlist (like a hard Gate)."""
    schemaVersion: str = "fit.combined@1"
    id: str = None
    searchRef: Ref = None
    listingRef: Ref = None
    memberFits: List[MemberFit] = None
    combined: Dict[str, Any] = None    # {score, method, agreement, dissent:[{personRef, theirScore, gap}]}
    vetoed: bool = None
    generatedAt: str = None
    vetoBy: Optional[List[Ref]] = None


@dataclass
class Vote:
    """vote@1 (§5.5c) — a veto/shortlist vote on a shared pursuit. `up`/`down` tally
    to order the shared shortlist; `veto` is a hard down that REQUIRES a reason
    (accountability — "vetoes welcome" paired with why) and is reversible."""
    schemaVersion: str = "vote@1"
    id: str = None
    pursuitRef: Ref = None
    searchRef: Ref = None
    listingRef: Ref = None
    personRef: Ref = None
    value: str = None                  # up | down | veto
    createdAt: str = None
    reason: Optional[str] = None       # REQUIRED for veto (validated in together.make_vote)


# ---------------------------------------------------------------------------
# P9 · Data & trust — the provenance render + the GDPR spine (09-data-trust.md
# §5.5 / §5.4). Every value on screen can say where it came from and how fresh;
# a Person can export and delete from day one. Licence attributions (OGL/ODbL)
# are rendered verbatim from source.registry@1.
# ---------------------------------------------------------------------------

@dataclass
class SourceRegistryEntry:
    """source.registry@1 row (§5.2a-registry) — the versioned truth about one data
    source: its licence and the verbatim attribution the render must show."""
    source: str = None
    label: str = None
    licence: str = None
    attribution: str = None            # verbatim; a {year} placeholder is filled at render
    ttlDays: Optional[int] = None
    cost: Optional[str] = None


@dataclass
class SourceLabel:
    """SourceLabel (§5.5a) — what every rendered number carries: where it came from,
    how fresh, and the licence attribution (verbatim from the registry)."""
    label: str = None
    source: str = None
    freshness: str = None
    attribution: str = None
    sourceDate: Optional[str] = None


@dataclass
class ClaimAttribution:
    """One 'show your work' row — a number/claim and the source(s) behind it. A
    null/miss renders "not available from {source}", never a blank (§5.5a rule 1)."""
    claim: str = None
    value: str = None
    sourceLabels: List[SourceLabel] = None
    note: Optional[str] = None


@dataclass
class ConsentRecord:
    """consent.record@1 (§5.4b) — captured before the first Person write. core_profiling
    is contract-basis; taste_twin_contribution is separately consented + revocable;
    analytics is off-by-default opt-in."""
    schemaVersion: str = "consent.record@1"
    personRef: Ref = None
    purposes: Dict[str, Any] = None
    policyVersion: str = None
    ipAtConsent: Optional[str] = None


@dataclass
class ExportBundle:
    """export.bundle@1 (§5.4c) — the one-click portable export (GDPR Art. 20). Because a
    Person is DESIGNED portable, the export is a product feature, not a compliance bolt-on:
    it IS the Person object plus its history. Shared property data is never included."""
    schemaVersion: str = "export.bundle@1"
    generatedAt: str = None
    person: Dict[str, Any] = None
    searches: List[Any] = None
    feedback: List[Any] = None
    consent: Dict[str, Any] = None
    ingestAddresses: List[Any] = None
    notIncluded: List[str] = None


# ---------------------------------------------------------------------------
# P8 · Outputs — market report + documents pack (08-action.md §5.2/§5.3). The
# things Gaff produces for you to use and share. Every report claim is sourced
# (P3 §5.7 inherited); the docpack assembles and stores but NEVER transacts or
# autofills (00-frame non-goals + the platform's prohibited-action rules, baked
# into the contract).
# ---------------------------------------------------------------------------

@dataclass
class ReportStat:
    """market.report@1 ReportSection.stats[] — one number, always sourced (A4)."""
    label: str = None
    value: Any = None
    sources: List[str] = None


@dataclass
class ReportEvidence:
    kind: str = None                   # comp | land_registry | flood | epc | listing_stat
    label: str = None
    value: Any = None
    ref: Optional[str] = None


@dataclass
class ReportSection:
    """market.report@1.sections[] (§5.2) — house-voice prose over sourced data. Every
    sentence that states a number maps to an evidence item; body invents no trend."""
    id: str = None
    kind: str = None                   # value_landscape | supply_liquidity | risk_landscape | ...
    title: str = None
    body: str = None
    stats: List[ReportStat] = None
    evidence: List[ReportEvidence] = None
    confidence: float = None


@dataclass
class MarketReport:
    """market.report@1 (§5.2) — a fully-sourced narrative over the Search's own scored
    data. LLM-authored in production (parked); the structure + the provenance lint are
    real. It presents the market as DATA and never issues personalised advice (A5)."""
    schemaVersion: str = "market.report@1"
    id: str = None
    searchRef: Ref = None
    scope: str = None                  # area | listing_context
    area: Dict[str, Any] = None        # {label, polygon}
    mode: Mode = None
    sections: List[ReportSection] = None
    headline: Dict[str, Any] = None    # {text, sources[]}
    dataWindow: Dict[str, Any] = None  # {from, to, freshestSource}
    provenance: Provenance = None
    confidence: float = None
    generatedAt: str = None
    engineModel: str = None
    listingRef: Optional[Ref] = None   # iff scope == listing_context


@dataclass
class DocItem:
    """docpack@1.items[] (§5.3a) — a required/optional document. `fileRef` points at a
    vault object the USER uploaded; Gaff stores the reference, never the secret value."""
    code: str = None
    label: str = None
    category: str = None               # identity|proof_of_address|funds|income|references|legal|guarantor
    required: bool = None
    status: str = None                 # missing | provided | verified | not_applicable
    source: str = None                 # user_upload | gaff_generated | third_party
    sensitivity: str = None            # high | normal
    fileRef: Optional[str] = None      # a vault reference, NOT the document contents
    note: Optional[str] = None


@dataclass
class GeneratedDoc:
    """docpack@1.generated[] — a document Gaff authored for the USER to send (a cover
    memo, a reference-request draft). Gaff drafts; the human sends."""
    code: str = None
    label: str = None
    kind: str = None                   # cover_memo | funds_summary | reference_request
    note: Optional[str] = None


@dataclass
class Docpack:
    """docpack@1 (§5.3) — the pre-assembled evidence pack. It ASSEMBLES and STORES; it
    never submits an application/offer to an agent, landlord, lender or portal (A6), and
    holds only user-managed file references + generated docs, no third-party secret
    values. Sharing is an explicit per-recipient user action; default private."""
    schemaVersion: str = "docpack@1"
    id: str = None
    personRef: Ref = None
    searchRef: Ref = None
    mode: str = None                   # buy | rent
    variant: str = None                # buy_mortgaged | buy_cash | rent_standard | rent_guarantor
    items: List[DocItem] = None
    readiness: Dict[str, Any] = None   # {pct, requiredTotal, requiredProvided, missing[]}
    sharePolicy: Dict[str, Any] = None # {default:"private", shares:[]}
    privacy: Dict[str, Any] = None     # {sensitivity:"high", exportable, retention, deletedAt}
    provenance: Dict[str, Any] = None  # {source:"user_capture", freshness, isDemo} — P8-local, not P1 Provenance
    createdAt: str = None
    updatedAt: str = None
    generated: Optional[List[GeneratedDoc]] = None
    pursuitRef: Optional[Ref] = None
