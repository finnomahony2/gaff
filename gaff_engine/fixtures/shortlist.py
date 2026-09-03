"""The M3 demo Buy shortlist — three De Beauvoir-street listings (09 provenance
``isDemo:true``) that populate the shortlist alongside the golden.

Honesty contract: these subject listings are ILLUSTRATIVE (marked ``isDemo`` —
the shell shows the demo badge), but each one's **Value Verdict is real** —
computed by the live engine from the actual HM Land Registry sold comps + EPC
£/sqft cached for that street (data/comps_enriched.json). They are priced to
sit below / around / above their anchor's trusted median so the shortlist shows
the truth layer *discriminating* — a steal, a fair, an over — which is the whole
point of the Buy differentiator.

MOVED 29 Aug 2026 (L2C): the "over" demo used to live on Mortimer Road, whose
anchor was 5+ trusted comps. The PPD transaction-category gate then exposed
three of them as "additional price paid" rows (repossession / power-of-sale),
leaving 3 open-market comps — so the comp-sufficiency gate honestly capped any
Mortimer tag to "fair" and the demo spread collapsed to steal/fair/fair/fair.
Rather than un-gate real repossessions to keep a story, the over demo moved to
De Beauvoir Road, where the street's own enriched set is thin (1 comp) and the
verdict honestly anchors on the area's 21 trusted open-market comps — an
"over" that stands on ≥5 comps with the anchor label saying exactly what it
stood on.

Each listing ships with its own recorded taste + forensics read (what a live
model would return), so the shortlist's taste and ranking vary believably rather
than every home reading as the golden. ``demo_shortlist()`` returns the
``(listing, taste_model, forensics_model)`` triples the M3 build scores.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from gaff_engine.schemas import (
    Address, Agent, BuyDetails, Chain, CouncilTax, Epc, GeoPoint, Listing, Media,
    Mode, Money, MoneyPeriod, PortalId, PriceEvent, PropertyType, Provenance,
    ProvenanceSource, Station, Tenure, TenureType,
)
from gaff_engine.taste import AxisRead, RecordedModel, TasteRead
from gaff_engine.forensics import RecordedForensicsModel
from gaff_engine.schemas import Forensics

_WEIGHTS = ["light_and_volume", "outdoor_space", "character_bones",
            "width_proportion_flow", "street_scene", "raw_size_threshold",
            "design_finish", "station_proximity"]


def _money(amount: int, period=MoneyPeriod.TOTAL) -> Money:
    return Money(amount=amount, currency="GBP", period=period)


def _demo_listing(lid, pid, street, incode, lat, lng, ptype, sqft, price,
                  tenure_type, lease_years, features, desc, service, ground,
                  ctax_band, ctax, epc_cur, epc_pot, listed_date, listed_price,
                  dom) -> Listing:
    return Listing(
        id=lid,
        listingKey=lid,                       # a stable demo key (real key not needed offline)
        portalIds=[PortalId(portal="example", id=pid,
                            url="https://listings.example.com/%s" % pid)],
        address=Address(display="%s, De Beauvoir, London N1" % street, line1=street,
                        outcode="N1", incode=incode, postcode="N1 " + incode, ukCountry="England"),
        mode=Mode.BUY,
        geo=GeoPoint(lat=lat, lng=lng, accuracy="accurate"),
        propertyType=ptype, beds=2, baths=2, receptions=2, sqft=sqft,
        keyFeatures=features, description=desc,
        images=[Media(url="https://media.example.com/%s-01.jpg" % pid, kind="photo",
                      caption="Reception")],
        floorplans=[Media(url="https://media.example.com/%s-fp.gif" % pid, kind="floorplan",
                          caption="Floorplan")],
        nearestStations=[Station(name="Dalston Junction", types=["OVERGROUND"], distanceMiles=0.5)],
        agent=Agent(branchName="Hackney", companyName="Example & Co"),
        buy=BuyDetails(
            price=_money(price, MoneyPeriod.TOTAL),
            tenure=Tenure(type=tenure_type, leaseYearsRemaining=lease_years),
            groundRent=_money(ground, MoneyPeriod.PA) if ground else None,
            serviceCharge=_money(service, MoneyPeriod.PA) if service else None,
            epc=Epc(current=epc_cur, rating="D" if epc_cur < 69 else "C", potential=epc_pot),
            councilTax=CouncilTax(band=ctax_band, annualEstimate=_money(ctax, MoneyPeriod.PA)),
            priceHistory=[PriceEvent(date=listed_date, event="listed", price=_money(listed_price)),
                          PriceEvent(date=listed_date, event="reduced", price=_money(price))]
            if listed_price != price else [PriceEvent(date=listed_date, event="listed", price=_money(price))],
            daysOnMarket=dom, chain=Chain.CHAIN_FREE, newBuild=False, soldComps=[]),
        derived=None,
        provenance=Provenance(source=ProvenanceSource.DEMO, portal="example",
                              fetchedAt="2026-07-14T08:00:00Z", freshness="fresh", isDemo=True,
                              completeness={"sqft": "stated"}),
    )


def _taste_read(scores: Dict[str, float], loves: List[str], staged: bool = False) -> TasteRead:
    axes = {k: AxisRead(scores[k], "%s read %.1f" % (k.replace("_", " "), scores[k])) for k in _WEIGHTS}
    return TasteRead(axes=axes, namedLoveHits=loves, antiSignalHits=[], staged=staged)


def _taste_model(image_scores, text_scores, loves, staged=False) -> RecordedModel:
    return RecordedModel({True: _taste_read(image_scores, loves, staged),
                          False: _taste_read(text_scores, loves, staged)})


def _forensics(listing_key, aspect, floor, ceiling) -> RecordedForensicsModel:
    f = Forensics(listingKey=listing_key, roomWidthsM=[3.6, 4.0], walkThroughBedroom=False,
                  hmoTells=False, cheapFlipSignals=[], aspect=aspect, ceilingHeightCue=ceiling,
                  floorPosition=floor)
    return RecordedForensicsModel({listing_key: f})


# --- The three demo listings -------------------------------------------------

# STEAL — Englefield Road (street median £959/sqft): 1150 sqft @ ~£810/sqft.
_ENGLEFIELD = _demo_listing(
    "listing_demo_englefield", "90000021", "Englefield Road", "5AB", 51.5441, -0.0862,
    PropertyType.CONVERSION, 1150, 931_000, TenureType.SHARE_OF_FREEHOLD, 999,
    ["Raised ground and garden maisonette", "Private east-facing garden",
     "Two double bedrooms, two bathrooms", "Exposed brick, restored floorboards",
     "Share of freehold", "Chain free"],
    "A broad Victorian conversion over the raised ground and garden floors of an "
    "Englefield Road terrace, with exposed brick, restored boards and a private garden.",
    1200, 0, "E", 2300, 64, 80, "2026-05-20", 975_000, 55)

# FAIR — Culford Road (median £998/sqft, only n=3 trusted → capped fair): 1000 sqft @ ~£1000/sqft.
_CULFORD = _demo_listing(
    "listing_demo_culford", "90000022", "Culford Road", "4HS", 51.5432, -0.0834,
    PropertyType.MAISONETTE, 1000, 999_000, TenureType.LEASEHOLD, 112,
    ["Two-bedroom period maisonette", "Balcony and shared garden",
     "Two bathrooms", "Original features", "Long lease", "Modern kitchen"],
    "A well-kept two-bedroom maisonette on Culford Road with a balcony, shared garden "
    "and a modern kitchen within a period conversion.",
    1600, 250, "D", 2200, 60, 75, "2026-06-10", 999_000, 34)

# OVER — De Beauvoir Road (area trusted median £910/sqft; the street's own
# enriched set is thin, so the real anchor is the area's 21 open-market
# comps): 1000 sqft @ £975/sqft, comfortably above the band on n >= 5.
_DEBEAUVOIR = _demo_listing(
    "listing_demo_debeauvoir", "90000023", "De Beauvoir Road", "4JP", 51.5406, -0.0851,
    PropertyType.FLAT, 1000, 975_000, TenureType.LEASEHOLD, 105,
    ["Two-bedroom apartment", "Two bathrooms", "Juliet balcony", "Allocated parking",
     "Long lease", "Recently refurbished"],
    "A recently refurbished two-bedroom apartment on De Beauvoir Road with a Juliet "
    "balcony and allocated parking, presented in move-in condition.",
    2400, 350, "C", 2400, 72, 82, "2026-04-15", 999_000, 90)


# A Yeate-style skinny archetype for the left-swipe WIDTH kill (§5.4a(2): "the
# design is nice … but too small, very skinny" — narrowness kills even when
# admired). 720 sqft, well under the 900 gate.
_YEATE = _demo_listing(
    "listing_demo_yeate", "90000024", "Yeate Street", "5RT", 51.5438, -0.0851,
    PropertyType.CONVERSION, 720, 720_000, TenureType.FREEHOLD, 999,
    ["Two-bedroom period terrace", "Considered interior", "Courtyard garden",
     "Original features", "Freehold"],
    "A characterful but narrow two-bedroom period terrace on Yeate Street with a "
    "considered interior and a small courtyard garden.",
    0, 0, "D", 2000, 62, 76, "2026-06-01", 720_000, 40)


def swipe_deck() -> List[Dict[str, Any]]:
    """A five-card cold-start deck for the front door + the M5 gate. Real
    archetype listings, each with a recorded taste read and the SCRIPTED gesture
    the deterministic build replays (the live app takes real gestures). The
    spread — distinctive · garden · ordinary · skinny-kill · grey-flip — gives
    the read the contrast §5.2 asks for, and exercises all three gestures
    (right / up / left) and both left kinds (a named-axis kill, an anti-signal)."""
    from gaff_engine.taste import canonical_model
    from gaff_engine.forensics import canonical_model as canonical_forensics
    from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING
    W = _WEIGHTS

    def rd(scores, loves=None, anti=None):
        read = TasteRead(axes={k: AxisRead(scores[k], "%s %.1f" % (k.replace("_", " "), scores[k])) for k in W},
                         namedLoveHits=loves or [], antiSignalHits=anti or [])
        return RecordedModel({True: read, False: read})

    return [
        {"listing": GOLDEN_LISTING, "taste_model": canonical_model(),
         "forensics_model": canonical_forensics(), "gesture": "right", "tier": "S",
         "offending_axis": None},
        {"listing": _ENGLEFIELD,
         "taste_model": rd({"light_and_volume": 8.0, "outdoor_space": 8.5, "character_bones": 8.5,
                            "width_proportion_flow": 8.0, "street_scene": 7.5, "raw_size_threshold": 7.0,
                            "design_finish": 6.5, "station_proximity": 7.0}, loves=["exposed brick"]),
         "forensics_model": _forensics("listing_demo_englefield", "east (rear)", "raised ground + garden", "generous"),
         "gesture": "right", "tier": "A", "offending_axis": None},
        {"listing": _CULFORD,
         "taste_model": rd({"light_and_volume": 7.0, "outdoor_space": 7.0, "character_bones": 7.5,
                            "width_proportion_flow": 7.0, "street_scene": 7.0, "raw_size_threshold": 7.0,
                            "design_finish": 7.0, "station_proximity": 7.0}),
         "forensics_model": _forensics("listing_demo_culford", "west (rear)", "first floor", "standard"),
         "gesture": "up", "tier": "B", "offending_axis": None},
        {"listing": _YEATE,
         "taste_model": rd({"light_and_volume": 7.5, "outdoor_space": 7.0, "character_bones": 8.0,
                            "width_proportion_flow": 2.5, "street_scene": 7.5, "raw_size_threshold": 4.0,
                            "design_finish": 7.0, "station_proximity": 7.0}),
         "forensics_model": _forensics("listing_demo_yeate", "north (rear)", "ground + first", "standard"),
         "gesture": "left", "tier": "A", "offending_axis": "width_proportion_flow"},
        {"listing": _DEBEAUVOIR,
         "taste_model": rd({"light_and_volume": 6.5, "outdoor_space": 5.5, "character_bones": 5.0,
                            "width_proportion_flow": 6.5, "street_scene": 6.5, "raw_size_threshold": 7.0,
                            "design_finish": 7.5, "station_proximity": 7.0},
                           anti=[("cheap/careless spec", -10.0, False)]),
         "forensics_model": _forensics("listing_demo_debeauvoir", "south (front)", "second floor", "standard"),
         "gesture": "left", "tier": "C", "offending_axis": None},
    ]


def demo_shortlist() -> List[Dict[str, Any]]:
    """The ``(listing, taste_model, forensics_model)`` triples the M3 build scores
    alongside the golden. Each verdict is real (real comps); each taste read is a
    per-listing recording so the shortlist varies believably."""
    return [
        {"listing": _ENGLEFIELD,
         "taste_model": _taste_model(
             {"light_and_volume": 7.5, "outdoor_space": 8.0, "character_bones": 8.0,
              "width_proportion_flow": 7.5, "street_scene": 7.5, "raw_size_threshold": 7.0,
              "design_finish": 6.5, "station_proximity": 7.0},
             {"light_and_volume": 7.0, "outdoor_space": 7.5, "character_bones": 7.5,
              "width_proportion_flow": 7.0, "street_scene": 7.0, "raw_size_threshold": 7.0,
              "design_finish": 6.0, "station_proximity": 7.0},
             ["exposed brick"]),
         "forensics_model": _forensics("listing_demo_englefield", "east (rear)",
                                       "raised ground + garden", "generous")},
        {"listing": _CULFORD,
         "taste_model": _taste_model(
             {"light_and_volume": 7.0, "outdoor_space": 6.5, "character_bones": 7.0,
              "width_proportion_flow": 7.0, "street_scene": 7.0, "raw_size_threshold": 7.0,
              "design_finish": 7.0, "station_proximity": 7.0},
             {"light_and_volume": 6.5, "outdoor_space": 6.5, "character_bones": 6.5,
              "width_proportion_flow": 7.0, "street_scene": 7.0, "raw_size_threshold": 7.0,
              "design_finish": 7.0, "station_proximity": 7.0},
             []),
         "forensics_model": _forensics("listing_demo_culford", "west (rear)",
                                       "first floor", "standard")},
        {"listing": _DEBEAUVOIR,
         "taste_model": _taste_model(
             {"light_and_volume": 6.5, "outdoor_space": 5.5, "character_bones": 5.0,
              "width_proportion_flow": 6.5, "street_scene": 6.5, "raw_size_threshold": 7.0,
              "design_finish": 7.5, "station_proximity": 7.0},
             {"light_and_volume": 6.5, "outdoor_space": 5.5, "character_bones": 5.5,
              "width_proportion_flow": 6.5, "street_scene": 6.5, "raw_size_threshold": 7.0,
              "design_finish": 7.5, "station_proximity": 7.0},
             [], staged=True),
         "forensics_model": _forensics("listing_demo_debeauvoir", "south (front)",
                                       "second floor", "standard")},
    ]


__all__ = ["demo_shortlist"]
