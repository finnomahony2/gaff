"""U9 EPC-adapter tests — the £/sqft floor-area layer.

DETERMINISTIC and NETWORK-FREE. No test ever touches the wire: the adapter's
cache dir is redirected to a throwaway temp dir seeded with the *confirmed*
synthetic fixtures (search rows for 104 Sample Road, N1 9ZY, and the
certificate 1000-2000-3000-4000-5001 whose ``total_floor_area`` is 40 m²). They
are authored to the register's response shape — address matching is what is
under test — so no real certificate or dwelling is reproduced here. The suite
every lookup runs with ``offline=True`` so a cache miss returns nothing rather
than fetching. The token file is never read.

Runnable two ways (matching tests/test_u9_landreg.py):

    python3 -m pytest tests/test_epc.py -v     # if pytest is installed
    python3 tests/test_epc.py                  # plain-stdlib PASS/FAIL fallback
"""

import json
import os
import sys
import tempfile

# Make the repo root importable whether run by pytest (from root) or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gaff_engine.epc as epc  # noqa: E402
from gaff_engine.epc import (  # noqa: E402
    _extract_rows, _house_matches, enrich_comps, fetch_certificate,
    floor_area_sqft_for, search_postcode, sqm_to_sqft,
)
from gaff_engine.schemas import Comp, CompAddress  # noqa: E402

# The confirmed certificate: 104b Sample Road, N1 9ZY -> total_floor_area 40 m².
CONFIRMED_CERT = "1000-2000-3000-4000-5001"

# Search rows for number 104 (four flats/re-lodgements); the most
# recent registrationDate (2025-11-26) is the confirmed cert.
_N1_9ZY_ROWS = [
    {"addressLine1": "104b, Sample Road", "addressLine2": None,
     "certificateNumber": CONFIRMED_CERT, "postcode": "N1 9ZY",
     "band": "D", "registrationDate": "2025-11-26"},
    {"addressLine1": "104c, Sample Road", "addressLine2": None,
     "certificateNumber": "1000-2000-3000-4000-5002", "postcode": "N1 9ZY",
     "band": "D", "registrationDate": "2019-12-05"},
    {"addressLine1": "104b, Sample Road", "addressLine2": None,
     "certificateNumber": "1000-2000-3000-4000-5003", "postcode": "N1 9ZY",
     "band": "E", "registrationDate": "2015-11-28"},
    {"addressLine1": "104a Sample Road", "addressLine2": None,
     "certificateNumber": "1000-2000-3000-4000-5004", "postcode": "N1 9ZY",
     "band": "E", "registrationDate": "2015-10-05"},
]

# Address histories (postcode N1 9ZZ) for the sale-date / area-change /
# confidence tests. 50 Test St = an extension (70→72→95 m²); 60 = stable (70→72);
# 70 = a single EPC (2015). Cert number -> (registrationDate, floor_area_m2).
_N1_9ZZ_CERTS = {
    "TEST-50-2012": ("2012-05-01", 70), "TEST-50-2018": ("2018-06-01", 72),
    "TEST-50-2023": ("2023-07-01", 95),
    "TEST-60-2016": ("2016-01-01", 70), "TEST-60-2020": ("2020-01-01", 72),
    "TEST-70-2015": ("2015-01-01", 80),
}
_N1_9ZZ_ADDR = {"TEST-50": "50, Test Street", "TEST-60": "60, Test Street",
                "TEST-70": "70, Test Street"}
_N1_9ZZ_ROWS = [
    {"addressLine1": _N1_9ZZ_ADDR[cn.rsplit("-", 1)[0]], "addressLine2": None,
     "certificateNumber": cn, "postcode": "N1 9ZZ", "band": "D",
     "registrationDate": rd}
    for cn, (rd, _m2) in _N1_9ZZ_CERTS.items()
]


def _install_offline_fixtures():
    """Redirect the adapter's cache at a temp dir seeded with offline fixtures.

    Writes the cache files through the adapter's own path helpers so the test can
    never drift from the real on-disk naming, then returns the temp dir. All
    lookups afterwards run with ``offline=True`` and read only these files.
    """
    tmp = tempfile.mkdtemp(prefix="epc_test_cache_")
    # Point BOTH cache tiers at the temp dir. Redirecting only the writable
    # one would leave reads falling through to the shipped warm cache, so a
    # "cache miss returns empty" assertion could pass or fail depending on
    # what happens to be seeded on the developer's machine.
    epc.EPC_CACHE_DIR = tmp   # monkeypatched globals are read at call time
    epc.SHIPPED_EPC_DIR = tmp

    with open(epc._search_cache_path("N1 9ZY"), "w", encoding="utf-8") as fh:
        json.dump({"postcode": "N1 9ZY", "count": len(_N1_9ZY_ROWS),
                   "rows": _N1_9ZY_ROWS}, fh)
    with open(epc._cert_cache_path(CONFIRMED_CERT), "w", encoding="utf-8") as fh:
        json.dump({"certificateNumber": CONFIRMED_CERT, "floor_area_m2": 40,
                   "habitable_rooms": 2, "address": "104b, Sample Road",
                   "postcode": "N1 9ZY"}, fh)

    # The N1 9ZZ synthetic histories (search rows + one cert file each, with area).
    with open(epc._search_cache_path("N1 9ZZ"), "w", encoding="utf-8") as fh:
        json.dump({"postcode": "N1 9ZZ", "count": len(_N1_9ZZ_ROWS),
                   "rows": _N1_9ZZ_ROWS}, fh)
    for cn, (rd, m2) in _N1_9ZZ_CERTS.items():
        with open(epc._cert_cache_path(cn), "w", encoding="utf-8") as fh:
            json.dump({"certificateNumber": cn, "floor_area_m2": m2,
                       "habitable_rooms": 3,
                       "address": _N1_9ZZ_ADDR[cn.rsplit("-", 1)[0]],
                       "postcode": "N1 9ZZ", "registrationDate": rd}, fh)
    return tmp


# Seed once at import; every test below is read-only + offline.
_TMP_CACHE = _install_offline_fixtures()


# ---------------------------------------------------------------------------
# 1. Address matching.
# ---------------------------------------------------------------------------

def test_address_match_number_and_suffix_rule():
    """EPC "104b, Sample Road" matches Land Registry paon "104" and "104B";
    it does NOT match "137". Suffix must be equal or absent on one side."""
    assert _house_matches("104", "104b, Sample Road") is True
    assert _house_matches("104B", "104b, Sample Road") is True
    assert _house_matches("137", "104b, Sample Road") is False

    # Exact suffixed match, and a mismatched suffix at the same number is rejected.
    assert _house_matches("137B", "137B, Sample Road") is True
    assert _house_matches("104A", "104b, Sample Road") is False
    # A named block (no leading number) can never match a numeric paon.
    assert _house_matches("104", "Dray House, Culford Road") is False
    assert _house_matches("SAMPLE COURT", "104b, Sample Road") is False


# ---------------------------------------------------------------------------
# 2. m² -> sqft.
# ---------------------------------------------------------------------------

def test_sqm_to_sqft_40_is_430_6():
    """40 m² converts to ~430.6 sqft (±0.5)."""
    assert abs(sqm_to_sqft(40) - 430.6) <= 0.5
    assert sqm_to_sqft(None) is None


def test_extract_rows_handles_both_payload_shapes():
    """search ``data`` may be a bare list OR a {"rows": [...]} dict — both parse."""
    assert _extract_rows({"data": _N1_9ZY_ROWS}) == _N1_9ZY_ROWS          # bare list
    assert _extract_rows({"data": {"rows": _N1_9ZY_ROWS}}) == _N1_9ZY_ROWS  # dict-with-rows
    assert _extract_rows({"data": None}) == []
    assert _extract_rows({}) == []


# ---------------------------------------------------------------------------
# 3. floor_area_sqft_for + enrich (fill where matched, None where not).
# ---------------------------------------------------------------------------

def test_floor_area_sqft_for_offline():
    """Offline lookup: "104"/"104B" at N1 9ZY resolve to ~430.6 sqft (the
    confirmed 40 m² cert, chosen as the most recent registration); "137" -> None."""
    assert abs(floor_area_sqft_for("104", "Sample Road", "N1 9ZY", offline=True) - 430.6) <= 0.5
    assert abs(floor_area_sqft_for("104B", "Sample Road", "N1 9ZY", offline=True) - 430.6) <= 0.5
    assert floor_area_sqft_for("137", "Sample Road", "N1 9ZY", offline=True) is None
    # A postcode with no cached search returns None (offline cache miss, no fetch).
    assert floor_area_sqft_for("104", "Sample Road", "SW1A 1AA", offline=True) is None


def _comp(paon, postcode, price):
    return Comp(price=price, date="2024-06-01",
                address=CompAddress(paon=paon, street="Sample Road",
                                    postcode=postcode),
                propertyType="flat-maisonette", tenure="Leasehold")


def test_enrich_fills_ppsf_where_matched_and_leaves_none_where_not():
    """A comp with a known EPC floor area gets a plausible pricePerSqft (>0);
    a comp with no EPC match keeps pricePerSqft None."""
    matched = _comp("104", "N1 9ZY", 900000)     # 900000 / 430.6 ~= 2090 £/sqft
    unmatched = _comp("999", "N1 9ZY", 800000)   # no cert for 999 -> None

    out = enrich_comps([matched, unmatched], offline=True)
    assert len(out) == 2
    a, b = out

    assert a.pricePerSqft is not None and a.pricePerSqft > 0
    assert abs(a.pricePerSqft - 900000 / 430.556) < 5.0   # ~2090.1 £/sqft
    assert b.pricePerSqft is None

    # Inputs are not mutated (enrich returns copies).
    assert matched.pricePerSqft is None and unmatched.pricePerSqft is None


# ---------------------------------------------------------------------------
# 4. Provenance.
# ---------------------------------------------------------------------------

def test_enriched_comp_carries_certnumber_and_sqft_provenance():
    """Enriched comps carry the epcCertNumber and the sqft used."""
    out = enrich_comps([_comp("104", "N1 9ZY", 900000)], offline=True)
    c = out[0]
    assert c.epcCertNumber == CONFIRMED_CERT
    assert c.sqft is not None and abs(c.sqft - 430.6) <= 0.5
    # £/sqft is self-consistent with the stamped sqft.
    assert abs(c.pricePerSqft - round(900000 / c.sqft, 1)) < 0.2

    # An unmatched comp carries no false provenance.
    miss = enrich_comps([_comp("999", "N1 9ZY", 800000)], offline=True)[0]
    assert miss.epcCertNumber is None and miss.sqft is None


def test_enrich_carries_transaction_category_through_replace():
    """landreg.parse_comp stamps the PPD transactionCategory as an INSTANCE
    attribute (the schema field is owned elsewhere), which dataclasses.replace
    drops — enrich_comps must copy it across on BOTH branches, or the Value
    scorer could never exclude a repossession from an enriched set."""
    matched = _comp("104", "N1 9ZY", 900000)
    matched.transactionCategory = "additional"
    unmatched = _comp("999", "N1 9ZY", 800000)
    unmatched.transactionCategory = "standard"
    plain = _comp("998", "N1 9ZY", 700000)               # no attribute: pre-capture
    a, b, c = enrich_comps([matched, unmatched, plain], offline=True)
    assert a.pricePerSqft is not None                     # the matched branch...
    assert a.transactionCategory == "additional"          # ...keeps the category
    assert b.pricePerSqft is None                         # the unmatched branch too
    assert b.transactionCategory == "standard"
    assert getattr(c, "transactionCategory", None) is None  # unknown stays unknown


# ---------------------------------------------------------------------------
# Sqft basis check — the SUBJECT's marketing sqft vs its EPC-derived area
# (the subject-side sibling of the comp area-change guard).
# ---------------------------------------------------------------------------

def test_sqft_basis_check_flags_beyond_tolerance_only():
    """Convention noise stays silent; a real disagreement (an extension the EPC
    predates, an eaves-counting floorplan) flags, with the diff measured
    against the EPC area (the certified reference)."""
    from gaff_engine.epc import SQFT_BASIS_TOLERANCE_PCT, sqft_basis_check
    assert SQFT_BASIS_TOLERANCE_PCT == 12.5              # mid of the 10-15% window
    close = sqft_basis_check(1000, 950)                  # 5.3% apart
    assert close is not None and close["conflict"] is False
    assert abs(close["diffPct"] - 5.3) < 0.05
    far = sqft_basis_check(1000, 700)                    # 42.9% apart
    assert far["conflict"] is True and abs(far["diffPct"] - 42.9) < 0.05
    # symmetric direction: an EPC LARGER than marketing flags just the same.
    assert sqft_basis_check(700, 1000)["conflict"] is True
    # the boundary itself is tolerated (beyond, not at).
    assert sqft_basis_check(1125, 1000)["conflict"] is False


def test_sqft_basis_check_never_fires_on_a_guess():
    """Either figure missing or non-positive -> None: no conflict can be
    asserted from one number, and junk input must not crash the verdict."""
    from gaff_engine.epc import sqft_basis_check
    assert sqft_basis_check(None, 700) is None
    assert sqft_basis_check(1000, None) is None
    assert sqft_basis_check(None, None) is None
    assert sqft_basis_check(0, 700) is None
    assert sqft_basis_check(1000, -5) is None
    assert sqft_basis_check("not a number", 700) is None


def test_fetch_certificate_offline_reads_cached_shape():
    """fetch_certificate(offline) returns the cached cert incl. floor_area_m2."""
    cert = fetch_certificate(CONFIRMED_CERT, offline=True)
    assert cert is not None
    assert cert["floor_area_m2"] == 40
    assert cert["certificateNumber"] == CONFIRMED_CERT
    # A never-cached cert returns None offline (no fetch).
    assert fetch_certificate("1000-2000-3000-4000-5999", offline=True) is None


def test_search_postcode_offline_reads_cache():
    """search_postcode(offline) returns the cached rows for a seeded postcode and
    [] for an unseeded one (never fetching)."""
    rows = search_postcode("N1 9ZY", offline=True)
    assert len(rows) == len(_N1_9ZY_ROWS)
    assert any(r["certificateNumber"] == CONFIRMED_CERT for r in rows)
    assert search_postcode("SW1A 1AA", offline=True) == []


# ---------------------------------------------------------------------------
# 5. Sale-date matching, staleness & area-change integrity (the M1 upgrade).
# ---------------------------------------------------------------------------

def _tcomp(paon, postcode, price, date):
    return Comp(price=price, date=date,
                address=CompAddress(paon=paon, street="Test Street",
                                    postcode=postcode),
                propertyType="flat-maisonette", tenure="Leasehold")


def test_sale_date_selection_picks_most_recent_pre_sale_epc():
    """50 Test St has EPCs 2012(70), 2018(72), 2023(95 m²). A 2019 sale must be
    divided by the 2018 area (as configured at sale) — NOT the later 2023 one."""
    sqft = floor_area_sqft_for("50", "Test Street", "N1 9ZZ",
                               sale_date="2019-03-01", offline=True)
    assert abs(sqft - 72 * 10.7639) < 0.5           # ~775.0 sqft = the 2018 area

    c = enrich_comps([_tcomp("50", "N1 9ZZ", 700000, "2019-03-01")], offline=True)[0]
    assert c.epcCertNumber == "TEST-50-2018"        # the most-recent PRE-sale EPC
    assert c.epcDate == "2018-06-01"
    assert c.epcCertNumber != "TEST-50-2023"        # not the newer post-sale EPC
    assert c.epcAfterSaleOnly is False


def test_after_sale_only_epc_flags_and_low_confidence():
    """A sale (2010) before every EPC at the address falls back to the earliest
    EPC, sets epcAfterSaleOnly and low confidence (a later EPC may be the buyer's
    renovation)."""
    c = enrich_comps([_tcomp("50", "N1 9ZZ", 600000, "2010-01-01")], offline=True)[0]
    assert c.epcAfterSaleOnly is True
    assert c.epcCertNumber == "TEST-50-2012"        # earliest available
    assert c.areaConfidence == "low"
    assert c.pricePerSqft is not None and c.pricePerSqft > 0


def test_area_change_detected_when_epcs_differ_beyond_threshold():
    """An address whose EPCs differ by >5 m² (70 vs 95) is flagged areaChanged with
    the min/max recorded; an address within threshold (70 vs 72) is not."""
    changed = enrich_comps([_tcomp("50", "N1 9ZZ", 700000, "2024-01-01")], offline=True)[0]
    assert changed.areaChanged is True
    assert changed.areaConfidence == "low"          # area-change forces low trust
    assert changed.epcAreaChange["minM2"] == 70 and changed.epcAreaChange["maxM2"] == 95
    assert changed.epcAreaChange["epcCount"] == 3

    stable = enrich_comps([_tcomp("60", "N1 9ZZ", 700000, "2024-01-01")], offline=True)[0]
    assert stable.areaChanged is False              # 70 vs 72 m² is within threshold
    assert stable.epcAreaChange is None


def test_area_confidence_downgrades_high_to_low_across_8y_gap():
    """Same single-EPC address (lodged 2015): a sale ~1y later is high confidence;
    a sale ~9y later crosses the 8y staleness line and drops to low."""
    near = enrich_comps([_tcomp("70", "N1 9ZZ", 500000, "2016-01-01")], offline=True)[0]
    far = enrich_comps([_tcomp("70", "N1 9ZZ", 500000, "2024-06-01")], offline=True)[0]

    assert near.areaConfidence == "high"            # gap ~1y, pre-sale, no area change
    assert near.epcAfterSaleOnly is False and near.areaChanged is False
    assert near.epcSaleGapYears <= 3

    assert far.areaConfidence == "low"              # gap ~9.4y > 8y
    assert far.epcSaleGapYears > 8
    # Same EPC underlies both — only the sale→EPC gap moved the confidence.
    assert near.epcCertNumber == far.epcCertNumber == "TEST-70-2015"


# ---------------------------------------------------------------------------
# T9 — cache schema versioning on the EPC cache.
# ---------------------------------------------------------------------------

def test_epc_cache_schema_mismatch_is_a_miss():
    """A future-shaped search file reads as a miss offline; the seeded
    (version-less) fixtures above keep reading as valid v1 throughout."""
    with open(epc._search_cache_path("N1 8XX"), "w", encoding="utf-8") as fh:
        json.dump({"cacheSchema": 999, "postcode": "N1 8XX",
                   "rows": [{"certificateNumber": "X"}]}, fh)
    assert search_postcode("N1 8XX", offline=True) == []


# ---------------------------------------------------------------------------
# T10 — token-story alignment (BACKLOG 2c): failure messages name the sources a
# package user actually has (env var, keychain, ~/.gaff), never the lab-only
# .secrets checkout path. The token itself is never read here.
# ---------------------------------------------------------------------------

def test_token_hint_names_real_sources_not_lab_path():
    hint = epc._token_hint()
    assert "GAFF_EPC_TOKEN" in hint
    assert "keychain" in hint
    assert "~/.gaff/epc_token" in hint
    assert ".secrets" not in hint


def test_http_error_message_is_token_free_and_names_real_sources():
    """The HTTPError re-raise stays token-free and points at the documented
    resolution order, not the lab .secrets path. urlopen and the token loader
    are stubbed, so nothing touches the wire or any token store."""
    import urllib.error

    def _boom(req, timeout=None):
        raise urllib.error.HTTPError("https://example.invalid/x", 401,
                                     "Unauthorized", hdrs=None, fp=None)

    saved = (epc._load_token, epc.urllib.request.urlopen, epc.REQUEST_PACING_SECONDS)
    epc._load_token = lambda: "not-a-real-token"
    epc.urllib.request.urlopen = _boom
    epc.REQUEST_PACING_SECONDS = 0
    try:
        try:
            epc._http_get_json("https://example.invalid/x")
        except RuntimeError as e:
            msg = str(e)
            assert "HTTP 401" in msg
            assert "GAFF_EPC_TOKEN" in msg and "~/.gaff/epc_token" in msg
            assert ".secrets" not in msg
            assert "not-a-real-token" not in msg     # never echo a token value
        else:
            raise AssertionError("expected RuntimeError from the 401 stub")
    finally:
        epc._load_token, epc.urllib.request.urlopen, epc.REQUEST_PACING_SECONDS = saved


def test_token_not_found_error_names_real_sources_not_lab_path():
    """The FIRST error a tokenless package user ever hits — the not-found
    raise out of ``_load_token`` — must name only the sources they can act on
    (env var, keychain, ~/.gaff), never the checkout-only .secrets path, and
    must suppress the resolver's original .secrets-naming exception so it
    cannot resurface in the traceback. The resolver is stubbed to raise the
    shape of its real message, so no token store is touched."""
    def _resolver_boom():
        raise RuntimeError(
            "EPC API token not found. Tried, in order:\n"
            "  - the GAFF_EPC_TOKEN environment variable\n"
            '  - the macOS keychain (service "gaff-epc-token")\n'
            "  - ~/.gaff/epc_token\n"
            "  - /somewhere/checkout/.secrets/epc_token"
        )

    saved = epc.paths.epc_token
    epc.paths.epc_token = _resolver_boom
    try:
        try:
            epc._load_token()
        except RuntimeError as e:
            msg = str(e)
            assert "token not found" in msg
            assert "GAFF_EPC_TOKEN" in msg
            assert "keychain" in msg
            assert "~/.gaff/epc_token" in msg
            assert ".secrets" not in msg
            # ``from None``: the original .secrets-naming error must not chain.
            assert e.__cause__ is None and e.__suppress_context__
        else:
            raise AssertionError("expected RuntimeError when no token source supplies one")
    finally:
        epc.paths.epc_token = saved


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest) — mirrors tests/test_u9_landreg.py.
# ---------------------------------------------------------------------------

def _run_standalone():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print("FAIL  %s\n      %s" % (name, e))
        except Exception as e:  # unexpected error
            failures += 1
            print("ERROR %s\n      %s: %s" % (name, type(e).__name__, e))
        else:
            print("PASS  %s" % name)
    print("-" * 60)
    total = len(tests)
    if failures:
        print("RESULT: FAIL (%d/%d passed, %d failed)" % (total - failures, total, failures))
    else:
        print("RESULT: PASS (%d/%d passed)" % (total, total))
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
