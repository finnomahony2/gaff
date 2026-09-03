"""T4 tests — the freeform input path: fields dict / pasted text → Listing.

DETERMINISTIC + OFFLINE: pure parsing, no network, no LLM. The adapter is the
package's answer to "how does a listing get in without a portal payload": an
LLM host supplies structured fields (the strong path), or a pasted blob goes
through the regex sweep (the fallback). Honesty contract: nothing is guessed;
every absence lands in provenance.completeness.

    python3 -m pytest tests/test_u10_freeform.py -v
    python3 tests/test_u10_freeform.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.ingest import (  # noqa: E402
    dedupe, listing_from_fields, listing_from_text, listing_key, parse_money,
)
from gaff_engine.schemas import (  # noqa: E402
    Listing, Mode, MoneyPeriod, PropertyType, TenureType,
)
from gaff_engine.validate import validate  # noqa: E402


RICH = {
    "mode": "buy", "price": "Guide Price £1,150,000",
    "address": "12 Sample Road, London N1 9ZY", "beds": 2, "baths": 2,
    "sqft": 1050, "property_type": "maisonette", "tenure": "share of freehold",
    "description": "A period maisonette with a skylit kitchen and a bay window.",
    "key_features": ["Bay window", "Skylit kitchen"],
    "lat": 51.54, "lng": -0.08,
    "image_urls": ["https://media.example.com/p1.jpg"],
    "agent_name": "Sample & Co", "agent_branch": "Islington",
}

PASTE = """12 Sample Road, London N1 9ZY
Guide Price £1,150,000. A charming two bedroom maisonette with 2 bathrooms,
1,050 sq ft, share of freehold, chain free, a skylit kitchen and a bay window.
89 years remaining on the lease."""


# ---------------------------------------------------------------------------
# 1 · The fields path (the MCP / LLM-host route).
# ---------------------------------------------------------------------------

def test_rich_fields_yield_a_contract_clean_listing():
    L = listing_from_fields(RICH, today="2026-08-29")
    assert isinstance(L, Listing)
    assert validate(L) == [], validate(L)
    assert L.mode == Mode.BUY and L.buy.price.amount == 1150000
    assert L.address.postcode == "N1 9ZY" and L.address.outcode == "N1"
    assert L.propertyType == PropertyType.MAISONETTE
    assert L.buy.tenure.type == TenureType.SHARE_OF_FREEHOLD
    assert L.provenance.source.value == "paste_link"


def test_sqm_derives_sqft_and_is_marked():
    L = listing_from_fields({"sqm": 100, "address": "A St, N1 1AA", "price": 500000})
    assert 1070 <= L.sqft <= 1080
    assert L.provenance.completeness["sqft"] == "derived"


def test_mode_inferred_from_rent_pcm():
    L = listing_from_fields({"rent_pcm": "£2,400 pcm", "address": "A St, N1 1AA"})
    assert L.mode == Mode.RENT and L.rent.rentPcm.amount == 2400
    assert L.buy is None


def test_unknown_tenure_string_degrades_to_unknown_not_a_crash():
    L = listing_from_fields({"tenure": "flying freehold?!", "price": 1,
                             "address": "A St, N1 1AA"})
    assert L.buy.tenure.type == TenureType.UNKNOWN


def test_absences_are_recorded_never_guessed():
    L = listing_from_fields({"address": "A St, N1 1AA"})
    c = L.provenance.completeness
    assert c["price"] == "missing" and c["sqft"] == "missing"
    assert c["tenure"] == "missing" and c["beds"] == "missing"


def test_identity_is_the_same_natural_key_as_normalise():
    """Same address+beds+postcode → same listingKey → dedupe interops across
    input paths."""
    a = listing_from_fields(RICH)
    b = listing_from_fields(dict(RICH, description="different words entirely"))
    assert a.listingKey == b.listingKey == listing_key(a)
    assert dedupe(b, {a.listingKey: [p.id for p in a.portalIds]}) == "duplicate_ignored"


def test_no_fabricated_portal_identity():
    """A paste must never mint a plausible real-portal listing number."""
    L = listing_from_fields(RICH)
    pid = L.portalIds[0]
    assert pid.portal == "paste"
    assert pid.id == L.listingKey[:12]        # derived from our own natural key


# ---------------------------------------------------------------------------
# 2 · The text path (the deterministic fallback).
# ---------------------------------------------------------------------------

def test_paste_parses_the_buy_essentials():
    L = listing_from_text(PASTE)
    assert L.mode == Mode.BUY and L.buy.price.amount == 1150000
    assert L.beds == 2 and L.baths == 2 and L.sqft == 1050
    assert L.address.postcode == "N1 9ZY"
    assert L.propertyType == PropertyType.MAISONETTE
    assert L.buy.tenure.type == TenureType.SHARE_OF_FREEHOLD
    assert L.buy.tenure.leaseYearsRemaining == 89
    assert L.address.display.startswith("12 Sample Road")
    assert L.description and "skylit kitchen" in L.description


def test_paste_parses_a_rent_listing():
    L = listing_from_text("Lovely three bed flat to rent, £2,400 pcm, N1 9ZY.")
    assert L.mode == Mode.RENT and L.rent.rentPcm.amount == 2400
    assert L.beds == 3 and L.propertyType == PropertyType.FLAT


def test_type_keyword_precedence():
    assert listing_from_text("An end of terrace house, £1, N1 1AA").propertyType \
        == PropertyType.END_TERRACE
    assert listing_from_text("A semi-detached house, £1, N1 1AA").propertyType \
        == PropertyType.SEMI_DETACHED
    assert listing_from_text("A detached house, £1, N1 1AA").propertyType \
        == PropertyType.DETACHED


def test_junk_paste_is_honest_not_wrong():
    L = listing_from_text("hello world")
    c = L.provenance.completeness
    assert c["price"] == "missing" and c["beds"] == "missing"
    assert L.buy.price is None and L.beds is None
    assert L.listingKey                                # identity still minted


def test_paste_never_invents_a_price_from_lease_years():
    L = listing_from_text("A fine house, 89 years remaining on the lease. N1 1AA")
    assert L.buy.price is None


# ---------------------------------------------------------------------------
# 2b · Adversarial-review regressions (the 16-findings pass, pinned).
# ---------------------------------------------------------------------------

def test_deposit_never_flips_a_rental_to_buy():
    L = listing_from_text("2 bed flat to rent £2,400 pcm. Deposit £5,000. "
                          "Holding deposit £550.")
    assert L.mode == Mode.RENT and L.rent.rentPcm.amount == 2400
    assert L.buy is None


def test_million_and_k_suffixes_expand():
    assert listing_from_text("Offers over £1.2m house, N1 1AA").buy.price.amount == 1200000
    assert listing_from_text("£450k flat, N1 1AA").buy.price.amount == 450000


def test_was_now_takes_the_now_price():
    L = listing_from_text("Was £1,200,000 now £1,150,000. Leasehold. N1 1AA")
    assert L.buy.price.amount == 1150000


def test_per_month_and_pw_only_are_rents():
    assert listing_from_text("Flat, £2,400 per month, N1 1AA").mode == Mode.RENT
    L = listing_from_text("Studio £560 pw, N1 1AA")
    assert L.mode == Mode.RENT and L.rent.rentPcm.amount == 2427
    assert L.provenance.completeness["rent_pcm"] == "derived"


def test_chain_free_reaches_the_schema():
    from gaff_engine.schemas import Chain
    assert listing_from_text("Chain free! £500,000, N1 1AA").buy.chain == Chain.CHAIN_FREE


def test_lease_years_alone_implies_leasehold():
    L = listing_from_text("Long lease with 125 years remaining. £300,000, N1 1AA")
    assert L.buy.tenure.type == TenureType.LEASEHOLD
    assert L.buy.tenure.leaseYearsRemaining == 125


def test_built_years_ago_is_not_a_lease():
    L = listing_from_text("Built 100 years ago, only two apartments left. "
                          "Freehold. £500,000. N1 1AA")
    assert L.buy.tenure.leaseYearsRemaining is None


def test_factless_pastes_do_not_collide():
    a = listing_from_text("x" * 200 + " castle text")
    b = listing_from_text("y" * 300 + " unrelated junk")
    assert a.listingKey != b.listingKey


def test_fields_path_coerces_what_a_host_would_send():
    assert listing_from_fields({"beds": "two", "address": "A St, N1 1AA"}).beds == 2
    assert listing_from_fields({"beds": "", "address": "A St, N1 1AA"}).beds is None
    L = listing_from_fields({"beds": {"weird": 1}, "address": "A St, N1 1AA"})
    assert L.beds is None and L.provenance.completeness["beds"] == "unparsed"
    assert listing_from_fields({"lease_years": "99 years", "tenure": "leasehold",
                                "price": 1, "address": "A, N1 1AA"}
                               ).buy.tenure.leaseYearsRemaining == 99
    one = listing_from_fields({"image_urls": "https://x/a.jpg", "price": 1,
                               "address": "A, N1 1AA"})
    assert len(one.images) == 1 and one.images[0].url == "https://x/a.jpg"


def test_ordinals_are_not_postcodes_and_trailing_outcodes_are():
    assert listing_from_text("Unit B1 2nd floor available").address.postcode is None
    assert listing_from_fields({"address": "Dalston, E8"}).address.outcode == "E8"


def test_fields_path_converts_a_pw_rent_string():
    """BACKLOG 1b P2, pinned: a per-week rent STRING passed as rent_pcm was
    stored unconverted and scored as monthly — a fair rental read as an
    implausible steal. The fields path must convert exactly like the text
    path, and say so."""
    L = listing_from_fields({"rent_pcm": "£550 pw", "address": "A St, N1 1AA"})
    assert L.mode == Mode.RENT
    assert L.rent.rentPcm.amount == 2383            # 550 * 52 / 12, rounded
    assert L.rent.rentPcm.period == MoneyPeriod.PCM
    assert L.provenance.completeness["rent_pcm"] == "derived"


def test_fields_path_sqm_string_derives_not_raises():
    """BACKLOG 1b P2, pinned: the sqft derivation re-read the raw field
    instead of the value _int_field just parsed, so "45 sqm" raised —
    against the adapter's own fail-soft contract."""
    L = listing_from_fields({"sqm": "45 sqm", "address": "A St, N1 1AA"})
    assert 480 <= L.sqft <= 490                     # 45 m² ≈ 484 sqft
    assert L.provenance.completeness["sqft"] == "derived"


def test_text_path_ordinal_does_not_mask_a_real_postcode():
    """BACKLOG 1b P2, pinned: the text path used raw _PC_RE, bypassing the
    ordinal guard — "Flat B2 2nd floor" poisoned the postcode AND masked the
    real one later in the paste."""
    L = listing_from_text("Flat B2 2nd floor, Sample Road, London N1 9ZY")
    assert L.address.postcode == "N1 9ZY"
    assert L.address.outcode == "N1"


def test_headline_first_line_yields_the_address_not_the_marketing():
    """BACKLOG 1b P2, pinned: a portal-headline first line became the street
    ("3 BED TERRACED HOUSE FOR SALE"), silently killing same-street comp
    anchoring — the engine's strongest evidence tier. The address must be
    the headline's address-shaped tail, and value's street read must anchor
    on the real street."""
    from gaff_engine.value import _subject_street
    L = listing_from_text(
        "3 bed terraced house for sale, De Beauvoir Road, London N1\n"
        "£850,000. A fine period terraced house.")
    assert L.address.display == "De Beauvoir Road, London N1"
    assert _subject_street(L) == "DE BEAUVOIR ROAD"
    assert L.beds == 3 and L.buy.price.amount == 850000


def test_headline_with_no_plausible_address_stays_missing():
    """Conservative half of the same fix: when the headline holds nothing
    address-shaped, the address stays honestly missing rather than wrong."""
    L = listing_from_text("2 bed flat to rent\n£2,000 pcm, available now.")
    assert L.address.display is None
    assert L.provenance.completeness["address"] == "missing"


def test_non_headline_first_line_still_becomes_the_address():
    """The guard must not eat real first-line addresses (the PASTE fixture
    shape): only listing-headline shapes are rejected."""
    L = listing_from_text("12 Sample Road, London N1 9ZY\nA fine flat. £500,000.")
    assert L.address.display.startswith("12 Sample Road")


def test_fields_path_converts_a_pa_rent_string():
    """Fixer pass, pinned: the pw fix's mirror image. parse_money already
    read "per annum", so a per-annum STRING in rent_pcm stored 24000 as the
    monthly figure — a fair rental read as wildly over."""
    L = listing_from_fields({"rent_pcm": "£24,000 per annum",
                             "address": "A St, N1 1AA"})
    assert L.mode == Mode.RENT
    assert L.rent.rentPcm.amount == 2000
    assert L.rent.rentPcm.period == MoneyPeriod.PCM
    assert L.provenance.completeness["rent_pcm"] == "derived"


def test_headline_for_sale_in_street_phrasing():
    """Fixer pass, pinned: Rightmove's dominant phrasing keeps the street
    INSIDE the headline segment ("for sale in De Beauvoir Road, London N1").
    Skipping that segment whole left "London N1" as the street — same-street
    comp anchoring dead for the most common portal shape."""
    from gaff_engine.value import _subject_street
    L = listing_from_text(
        "3 bedroom terraced house for sale in De Beauvoir Road, London N1\n"
        "£850,000. A fine period terraced house.")
    assert L.address.display == "De Beauvoir Road, London N1"
    assert _subject_street(L) == "DE BEAUVOIR ROAD"
    assert L.beds == 3 and L.buy.price.amount == 850000


def test_uppercase_incode_ending_st_is_a_postcode_not_an_ordinal():
    """Fixer pass, pinned: S/T/N/D/R/H are legal incode letters, so the
    case-blind ordinal guard rejected real postcodes like "BS1 4ST". The
    discriminator is CONTEXT (storey/flat-id neighbours), never case."""
    L = listing_from_text("3rd floor flat, 10 High Street, Bristol BS1 4ST")
    assert L.address.postcode == "BS1 4ST"
    assert L.address.outcode == "BS1"
    # the ordinal-in-storey-context shape stays rejected, in any case
    assert listing_from_text("Unit B1 2nd floor available").address.postcode is None


def test_ordinal_guard_is_context_based_not_case_based():
    """L2 fixer pass, pinned both ways: case discrimination alone re-opened
    bug 1b for ALL-CAPS portal address blocks ("FLAT B2 2ND" read as a
    postcode, masking the real one later in the paste) and dropped genuine
    all-lowercase postcodes ("bs1 4st" read as an ordinal)."""
    L = listing_from_text("FLAT B2 2ND FLOOR, SAMPLE ROAD, LONDON N1 9ZY\n"
                          "2 bed flat. £600,000")
    assert L.address.postcode == "N1 9ZY"                 # the real one wins
    assert L.address.outcode == "N1"
    lower = listing_from_text("21 st marys road, bristol bs1 4st\n"
                              "2 bed flat. £300,000")
    assert lower.address.postcode == "BS1 4ST"            # not lost as an ordinal


def test_rent_pcm_is_stored_with_period_pcm_on_every_path():
    """Fixer pass, pinned: a numeric rent_pcm (fields path, and the text
    path's pw-derived int) inherited Money's TOTAL default — the stored
    record misstated the fact the field name asserts."""
    assert listing_from_fields({"rent_pcm": 2000, "address": "A, N1 1AA"}
                               ).rent.rentPcm.period == MoneyPeriod.PCM
    assert listing_from_text("Studio £560 pw, N1 1AA"
                             ).rent.rentPcm.period == MoneyPeriod.PCM


def test_k_suffix_survives_an_abutting_period_token():
    """Fixer pass, pinned: the m/k lookahead that protects "£450km" also
    killed the suffix when a rent unit abutted it — "£3kpcm" read as £3."""
    money, note = parse_money("£3kpcm")
    assert note is None
    assert money.amount == 3000 and money.period == MoneyPeriod.PCM
    # the protections the lookahead exists for still hold
    assert parse_money("£450km")[0].amount == 450
    assert parse_money("£3k pcm")[0].amount == 3000


def test_fractional_sqm_rounds_once_after_conversion():
    """Fixer pass, pinned: "97.5 sq m" was truncated to 97 BEFORE the sqft
    multiply (1044, not 1049); the derivation must round once, at the end."""
    L = listing_from_fields({"sqm": "97.5 sq m", "address": "A, N1 1AA"})
    assert L.sqft == 1049
    assert L.provenance.completeness["sqft"] == "derived"


def test_qualifier_lands_on_the_money():
    from gaff_engine.schemas import PriceQualifier
    L = listing_from_text("Guide price £500,000, N1 1AA")
    assert L.buy.price.qualifier == PriceQualifier.GUIDE


def test_let_synonym_and_ambiguous_price_note():
    assert listing_from_fields({"mode": "let", "rent_pcm": 2000,
                                "address": "A, N1 1AA"}).mode == Mode.RENT
    L = listing_from_text("Plot A £300,000. Plot B £350,000. N1 1AA")
    assert L.provenance.completeness.get("price") == "ambiguous"


# ---------------------------------------------------------------------------
# 3 · The point of it all: a pasted listing scores through the real engine.
# ---------------------------------------------------------------------------

def test_text_parsed_listing_scores_end_to_end():
    from gaff_engine.taste import AxisRead, RecordedModel, TasteRead, taste_result

    L = listing_from_text(PASTE)
    axes = {k: AxisRead(7.0, "%s read" % k) for k in (
        "light_and_volume", "outdoor_space", "character_bones",
        "width_proportion_flow", "street_scene", "raw_size_threshold",
        "design_finish", "station_proximity")}
    read = TasteRead(axes=axes, namedLoveHits=None, antiSignalHits=[], staged=False)
    person = {"taste": {"weights": {k: 5 for k in axes},
                        "lovesNamed": ["skylit kitchens"]}}
    tr = taste_result(L, person, RecordedModel({True: read, False: read}))
    assert tr.score >= 7.0                              # base 7.0 + the named love
    love = [a for a in tr.tasteAdjustments if a.kind == "named_love"]
    assert love and love[0].delta == 0.1                # skylit kitchen hit from the PASTE text


# ---------------------------------------------------------------------------
# Plain-stdlib runner.
# ---------------------------------------------------------------------------

def _run_standalone():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print("FAIL  %s\n      %s" % (name, e))
        except Exception as e:
            failures += 1
            print("ERROR %s\n      %s: %s" % (name, type(e).__name__, e))
        else:
            print("PASS  %s" % name)
    print("-" * 60)
    total = len(tests)
    print("RESULT: %s (%d/%d passed%s)" % (
        "FAIL" if failures else "PASS", total - failures, total,
        ", %d failed" % failures if failures else ""))
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
