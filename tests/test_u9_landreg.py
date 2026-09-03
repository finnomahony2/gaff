"""U9 tests — the HM Land Registry Price Paid comps adapter.

DETERMINISTIC: never hits the network. The parse / like-for-like tests use
in-file raw fixtures; the integration + guard tests read the on-disk cache
under ``data/comps/`` with ``offline=True`` (a cache miss returns ``[]`` rather
than fetching), so the run is reproducible.

Runnable two ways (matching tests/test_u1_golden.py):

    python3 -m pytest tests/test_u9_landreg.py -v     # if pytest is installed
    python3 tests/test_u9_landreg.py                  # plain-stdlib fallback
"""

import os
import re
import sys

# Make the repo root importable whether run by pytest (from root) or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.landreg import (  # noqa: E402
    SHIPPED_CACHE_DIR, comps_for_listing, parse_comp, select_like_for_like,
    _lang_value, _parse_date, _txn_category,
)
from gaff_engine.schemas import Comp, PropertyType  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING  # noqa: E402

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A faithful raw PPD ``result.items[]`` item — the real £855k, 28-Apr-2026
# Northchurch Road leasehold flat-maisonette. Note propertyType / estateType are
# *dicts* carrying ``_about`` + ``label``/``prefLabel`` langString lists (the
# observed shape), NOT bare strings.
RAW_855K = {
    "pricePaid": 855000,
    "transactionDate": "Tue, 28 Apr 2026",
    "transactionId": "AAAA-855K-NORTHCHURCH",
    "newBuild": False,
    "propertyAddress": {
        "paon": "137B", "street": "NORTHCHURCH ROAD", "postcode": "N1 3NT",
        "district": "HACKNEY", "town": "LONDON", "county": "GREATER LONDON",
    },
    "propertyType": {
        "_about": "http://landregistry.data.gov.uk/def/common/flat-maisonette",
        "label": [{"_value": "Flat-maisonette", "_lang": "en", "_datatype": "langString"}],
        "prefLabel": [{"_value": "flat-maisonette", "_lang": "en", "_datatype": "langString"}],
    },
    "estateType": {
        "_about": "http://landregistry.data.gov.uk/def/common/leasehold",
        "label": [{"_value": "Leasehold", "_lang": "en", "_datatype": "langString"}],
        "prefLabel": [{"_value": "Leasehold", "_lang": "en", "_datatype": "langString"}],
    },
    "transactionCategory": {
        "_about": "http://landregistry.data.gov.uk/def/ppi/standardPricePaidTransaction",
        "label": [{"_value": "Standard price paid transaction", "_lang": "en",
                   "_datatype": "langString"}],
        "prefLabel": [{"_value": "Standard price paid transaction", "_lang": "en",
                       "_datatype": "langString"}],
    },
}


def test_parse_known_item():
    """A known raw item parses to a Comp: ISO date, int price, type/tenure ._value
    extracted (never str(dict)), address parts, and pricePerSqft None."""
    comp = parse_comp(RAW_855K, distance_note="same street")
    assert isinstance(comp, Comp)
    assert comp.price == 855000 and isinstance(comp.price, int)
    assert comp.date == "2026-04-28"                    # "Tue, 28 Apr 2026" -> ISO
    assert comp.propertyType == "flat-maisonette"       # prefLabel ._value, not the dict
    assert "_about" not in (comp.propertyType or "") and "{" not in (comp.propertyType or "")
    assert comp.tenure == "Leasehold"                   # estateType ._value
    assert comp.newBuild is False
    assert comp.address.paon == "137B"
    assert comp.address.street == "NORTHCHURCH ROAD"
    assert comp.address.postcode == "N1 3NT"
    assert comp.distanceNote == "same street"
    assert comp.pricePerSqft is None                    # Land Registry £/sqft gap
    assert comp.schemaVersion == "comp@1" and comp.source == "hm_land_registry"
    assert comp.transactionCategory == "standard"       # PPD category captured


def test_lang_value_handles_both_shapes_and_dates():
    """_lang_value copes with the recon 'bare list' shape too; dates normalise."""
    # Recon shape: a bare list of langString dicts.
    assert _lang_value([{"_value": "terraced", "_lang": "en", "_datatype": "langString"}]) == "terraced"
    # Observed shape: a dict with prefLabel wins the lowercase slug.
    assert _lang_value(RAW_855K["propertyType"]) == "flat-maisonette"
    # A bare _about URI falls back to its slug.
    assert _lang_value({"_about": "http://x/def/common/detached"}) == "detached"
    # Dates: RFC-822, bare, and already-ISO all normalise; empty -> None.
    assert _parse_date("Tue, 28 Apr 2026") == "2026-04-28"
    assert _parse_date("28 Apr 2026") == "2026-04-28"
    assert _parse_date("2026-04-28") == "2026-04-28"
    assert _parse_date(None) is None


def test_txn_category_normalises_every_observed_shape():
    """The category node is the same langString-dict shape as propertyType:
    prefLabel text, bare _about slug, and absence all normalise; anything
    unrecognisable reads as unknown (None), never a guess."""
    assert _txn_category(RAW_855K["transactionCategory"]) == "standard"
    assert _txn_category({
        "_about": "http://landregistry.data.gov.uk/def/ppi/additionalPricePaidTransaction",
        "prefLabel": [{"_value": "Additional price paid transaction"}],
    }) == "additional"
    # a bare _about URI (no labels) still resolves via its slug.
    assert _txn_category(
        {"_about": "http://x/def/ppi/standardPricePaidTransaction"}) == "standard"
    assert _txn_category(
        {"_about": "http://x/def/ppi/additionalPricePaidTransaction"}) == "additional"
    assert _txn_category(None) is None
    assert _txn_category({"prefLabel": [{"_value": "Something else entirely"}]}) is None


def test_parse_comp_missing_category_reads_unknown():
    """A raw item with no transactionCategory (a pre-capture record) parses to
    category None — unknown, which downstream treats as standard but counts."""
    item = {k: v for k, v in RAW_855K.items() if k != "transactionCategory"}
    comp = parse_comp(item)
    assert comp.transactionCategory is None


def _mixed_comps():
    """A synthetic mix of houses + flats for the like-for-like filter."""
    def mk(ptype, tenure, price):
        return parse_comp({
            "pricePaid": price, "transactionDate": "Wed, 01 Jan 2025",
            "transactionId": "%s-%s-%d" % (ptype, tenure, price), "newBuild": False,
            "propertyAddress": {"paon": "1", "street": "TEST ROAD", "postcode": "N1 1AA"},
            "propertyType": {"prefLabel": [{"_value": ptype}]},
            "estateType": {"prefLabel": [{"_value": tenure}]},
        })
    return [
        mk("flat-maisonette", "Leasehold", 800000),
        mk("flat-maisonette", "Freehold", 900000),
        mk("terraced", "Freehold", 1400000),
        mk("semi-detached", "Freehold", 1600000),
        mk("detached", "Leasehold", 2000000),
        mk("other", "Leasehold", 500000),
    ]


def test_like_for_like_maisonette_returns_only_flats():
    """Given a mix of houses + flats, a maisonette target keeps only
    flat-maisonette comps (PPD lumps flats + maisonettes into one slug)."""
    comps = _mixed_comps()
    lfl = select_like_for_like(comps, "maisonette")
    assert len(lfl) == 2
    assert all(c.propertyType == "flat-maisonette" for c in lfl)
    assert not any(c.propertyType in ("terraced", "semi-detached", "detached", "other") for c in lfl)

    # Enum target + tenure narrows to the leasehold flat-maisonette only.
    lfl_lease = select_like_for_like(comps, PropertyType.MAISONETTE, "leasehold")
    assert len(lfl_lease) == 1
    assert lfl_lease[0].propertyType == "flat-maisonette" and lfl_lease[0].tenure == "Leasehold"

    # A house target mirrors: houses only, no flats.
    houses = select_like_for_like(comps, "terraced")
    assert len(houses) == 3
    assert all(c.propertyType in ("terraced", "semi-detached", "detached") for c in houses)
    assert not any(c.propertyType == "flat-maisonette" for c in houses)


def test_integration_on_cache_de_beauvoir():
    """comps_for_listing reads the cached De Beauvoir data (offline, no network)
    and returns a non-empty list including >=1 same-street leasehold
    flat-maisonette; the £855k Apr-2026 Northchurch Road maisonette is present."""
    assert os.path.isdir(SHIPPED_CACHE_DIR), (
        "shipped warm cache %s missing — run the U9 fetch (comps_for_listing live) "
        "first" % SHIPPED_CACHE_DIR)
    comps = comps_for_listing(GOLDEN_LISTING, since_year=2021, offline=True)
    assert comps, "expected cached De Beauvoir comps; run the U9 fetch to populate data/comps/"

    lfl = select_like_for_like(comps, GOLDEN_LISTING.propertyType, "leasehold")
    same_street_flats = [c for c in lfl if c.distanceNote == "same street"]
    assert same_street_flats, "expected >=1 same-street leasehold flat-maisonette comp"

    golden = [c for c in comps
              if c.price == 855000 and c.date == "2026-04-28"
              and c.propertyType == "flat-maisonette" and c.tenure == "Leasehold"]
    assert golden, "the £855k 2026-04-28 Northchurch Road maisonette must be in the cached comps"
    assert (golden[0].address.street or "").upper() == "NORTHCHURCH ROAD"
    assert golden[0].distanceNote == "same street"


def test_guard_every_comp_ppsf_none_and_iso_date():
    """Guard: every cached Comp documents the EPC gap (pricePerSqft is None) and
    carries a valid ISO date on/after the since_year filter."""
    comps = comps_for_listing(GOLDEN_LISTING, since_year=2021, offline=True)
    assert comps
    for c in comps:
        assert c.pricePerSqft is None, "pricePerSqft must be None (EPC-register gap): %r" % (c,)
        assert c.date and _ISO.match(c.date), "date must be ISO YYYY-MM-DD, got %r" % c.date
        assert int(c.date[:4]) >= 2021, "since_year filter breached: %r" % c.date


def test_cached_comps_carry_transaction_category():
    """The shipped raw cache stores items verbatim, so every parsed comp gets a
    recognised category — and the De Beauvoir set genuinely contains both kinds
    (the 'additional' rows are the ones the Value scorer must not anchor on)."""
    comps = comps_for_listing(GOLDEN_LISTING, since_year=2021, offline=True)
    assert comps
    cats = {getattr(c, "transactionCategory", None) for c in comps}
    assert cats <= {"standard", "additional"}, cats     # never an unparsed shape
    assert "standard" in cats
    assert "additional" in cats


def test_transaction_category_survives_persist_roundtrip():
    """parse -> enrich-style persist -> load must keep the category. The trap:
    the category rides as an INSTANCE attribute on Comp, and
    serialize.to_jsonable emits declared dataclass fields only — so a bare
    to_jsonable persist silently strips it and the exclusion goes inert on the
    enriched-file path (the P1 this pins). enrich_run.comp_payload is the
    persist seam that carries it explicitly; value._comp_from_dict is the load
    seam that restores it."""
    try:
        from enrich_run import comp_payload
    except ImportError:
        # enrich_run is a lab script build_public deliberately excludes; the
        # persist seam it owns cannot run in the assembled package, so the
        # shipped suite skips rather than failing a wheel user.
        print("SKIP test_transaction_category_survives_persist_roundtrip "
              "(enrich_run is lab-only and does not ship)")
        return
    from gaff_engine.serialize import to_jsonable
    from gaff_engine.value import _comp_from_dict

    additional = dict(RAW_855K)
    additional["transactionCategory"] = {
        "_about": "http://landregistry.data.gov.uk/def/ppi/additionalPricePaidTransaction",
        "prefLabel": [{"_value": "Additional price paid transaction"}],
    }
    comp = parse_comp(additional, distance_note="same street")
    assert comp.transactionCategory == "additional"

    # The regression: to_jsonable alone drops the instance attribute...
    assert "transactionCategory" not in to_jsonable(comp)
    # ...so the persist seam must carry it, and the loader must restore it.
    payload = comp_payload(comp)
    assert payload["transactionCategory"] == "additional"
    assert _comp_from_dict(payload).transactionCategory == "additional"
    # A pre-capture comp persists an explicit null and loads back as unknown.
    bare = parse_comp({k: v for k, v in RAW_855K.items()
                       if k != "transactionCategory"})
    bare_payload = comp_payload(bare)
    assert bare_payload["transactionCategory"] is None
    assert getattr(_comp_from_dict(bare_payload), "transactionCategory", None) is None


# ---------------------------------------------------------------------------
# T5 — the live fetch is guarded: failures are audible and fail-soft.
# ---------------------------------------------------------------------------

def test_fetch_street_network_failure_warns_and_returns_empty():
    """A dead upstream yields [] plus a RuntimeWarning naming street and cause —
    never a raw traceback, never a silent []."""
    import urllib.error
    import urllib.request
    import warnings as _w
    import gaff_engine.landreg as L

    real = urllib.request.urlopen
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")
    urllib.request.urlopen = boom
    try:
        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            items = L.fetch_street("NOWHERE STREET", "NOWHERE TOWN", force=True)
        assert items == []
        assert len(caught) == 1 and issubclass(caught[0].category, RuntimeWarning)
        msg = str(caught[0].message)
        assert "NOWHERE STREET" in msg and "URLError" in msg
    finally:
        urllib.request.urlopen = real


def test_fetch_street_malformed_response_warns_and_returns_empty():
    """Upstream returning non-JSON is the same failure class as no upstream."""
    import io as _io
    import urllib.request
    import warnings as _w
    import gaff_engine.landreg as L

    class FakeResp(_io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    real = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: FakeResp(b"<html>maintenance</html>")
    try:
        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            items = L.fetch_street("NOWHERE STREET", "NOWHERE TOWN", force=True)
        assert items == [] and len(caught) == 1
    finally:
        urllib.request.urlopen = real


def test_fetch_street_corrupt_cache_falls_through_not_raises():
    """A corrupt cache file is skipped (offline -> []), not a JSONDecodeError."""
    import tempfile
    import gaff_engine.landreg as L
    from gaff_engine.landreg import _slug

    tmp = tempfile.mkdtemp()
    old_user, old_ship = L.CACHE_DIR, L.SHIPPED_CACHE_DIR
    L.CACHE_DIR = L.SHIPPED_CACHE_DIR = tmp
    try:
        path = L._cache_path("BAD STREET", "TESTTOWN")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not json")
        assert L.fetch_street("BAD STREET", "TESTTOWN", offline=True) == []
    finally:
        L.CACHE_DIR, L.SHIPPED_CACHE_DIR = old_user, old_ship


# ---------------------------------------------------------------------------
# T9 — cache schema versioning: mismatch is a miss, missing field is valid v1.
# ---------------------------------------------------------------------------

def test_cache_schema_mismatch_is_a_miss_and_missing_field_is_valid():
    import json as _json
    import tempfile
    import gaff_engine.landreg as L

    tmp = tempfile.mkdtemp()
    old_user, old_ship = L.CACHE_DIR, L.SHIPPED_CACHE_DIR
    L.CACHE_DIR = L.SHIPPED_CACHE_DIR = tmp
    try:
        # A future-shaped file must NOT be parsed on hope.
        p = L._cache_path("VER STREET", "TESTTOWN")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        _json.dump({"cacheSchema": 999, "town": "TESTTOWN",
                    "items": [{"pricePaid": 1}]}, open(p, "w"))
        assert L.fetch_street("VER STREET", "TESTTOWN", offline=True) == []
        # A pre-versioning file (no field) is the v1 shape and still reads.
        _json.dump({"town": "TESTTOWN", "items": [{"pricePaid": 2}]}, open(p, "w"))
        assert L.fetch_street("VER STREET", "TESTTOWN", offline=True) == [{"pricePaid": 2}]
    finally:
        L.CACHE_DIR, L.SHIPPED_CACHE_DIR = old_user, old_ship


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest) — mirrors tests/test_u1_golden.py.
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
