"""B1 — the UK HPI adapter, tested OFFLINE against the committed ``data/hpi/`` cache.

Never hits the network: every ``hpi_factor`` call here passes ``offline=True``, so a
month that isn't cached simply yields no adjustment (factor 1.0). The Hackney months
the golden comps use are committed, so the real factors are exercised deterministically.

    python3 tests/test_hpi.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.hpi import (  # noqa: E402
    AS_OF_MONTH, avg_price, hpi_factor, month_of, normalise_type, region_for,
)


def test_month_of_buckets():
    assert month_of("2021-03-15") == "2021-03"
    assert month_of("2024-06") == "2024-06"
    assert month_of(None) is None and month_of("garbage") is None


def test_type_normalisation_defaults_to_flats():
    assert normalise_type("flat-maisonette") == "averagePriceFlatMaisonette"
    assert normalise_type("Terraced") == "averagePriceTerraced"
    assert normalise_type("semi-detached") == "averagePriceSemiDetached"
    assert normalise_type("anything else") == "averagePriceFlatMaisonette"


def test_region_for_maps_a_known_area():
    """A mapped London area, and the second warm-cache city, each resolve to
    their OWN district — a Leamington verdict must never say "(UK HPI, london)"."""
    assert region_for({"address": "12 De Beauvoir Road, London N1"}) == "hackney"
    assert region_for({"borough": "Islington"}) == "islington"
    assert region_for({"address": "Sample Road, Leamington Spa CV31"}) == "warwick"


def test_region_for_abstains_when_it_cannot_place_the_subject():
    """The unmapped town returns None, not "london".

    This is the whole point of the change. The old fallback made every
    unrecognised address adjust in the London series, which is a real
    adjustment in the wrong market: off the committed cache, a Feb 2021 flat
    moves +2.4% and a Jun 2022 one -3.6%, so an unplaceable subject's comps
    were being pushed either way by London's curve. It stayed invisible until
    S5 began printing the region on every verdict, where it would have read
    "(london)" under a Yorkshire address.
    """
    for subject in ({"address": "42 Kirkstall Road, Leeds LS3"},
                    {"address": "1 Deansgate, Manchester M3"},
                    {"address": "a place with no known area"},
                    {"address": ""},
                    {}):
        assert region_for(subject) is None, subject


def test_an_explicit_district_is_the_subjects_own_region():
    """A borough/district field is used even when the area map has never heard
    of it: it is the subject's OWN district, slugified the way UK HPI names
    districts. This is the path tools._routed_comps puts a newly warmed town
    on (it attaches listing.district), which is why abstaining above costs a
    warmed town nothing.

    It is fail-closed regardless: an unrecognised slug fetches nothing, so the
    factor stays 1.0 rather than becoming somewhere else's number."""
    assert region_for({"district": "Leeds"}) == "leeds"
    assert region_for({"district": "Stratford-on-Avon"}) == "stratford-on-avon"
    assert region_for({"district": "!!!"}) is None       # slugifies to nothing
    assert hpi_factor(region_for({"district": "Leeds"}), "flat",
                      "2021-01-01", offline=True) == 1.0


def test_abstaining_costs_no_adjustment_rather_than_a_wrong_one():
    """None flows through hpi_factor as 1.0 — comps stand in the money of their
    own sale dates. Contrast the old behaviour, still reachable by naming the
    region explicitly, which moved a subject's comps in London's series."""
    assert hpi_factor(None, "flat", "2021-02-01", offline=True) == 1.0
    assert hpi_factor(region_for({"address": "Leeds LS3"}), "flat",
                      "2021-02-01", offline=True) == 1.0
    # The adjustment the fallback used to apply to any unplaced subject, read
    # from the committed cache (2021-02 is present for both regions; 2021-01 is
    # NOT committed for london, so asserting on it would silently read whatever
    # the developer has in ~/.gaff).
    assert hpi_factor("london", "flat", "2021-02-01", offline=True) > 1.02
    assert hpi_factor("hackney", "flat", "2021-02-01", offline=True) != 1.0


def test_factor_reads_the_committed_cache_offline():
    """Hackney flats softened into 2025 — a 2024 comp adjusts to BELOW 1.0, read from
    the committed cache with no network."""
    f = hpi_factor("hackney", "flat-maisonette", "2024-06-01", offline=True)
    assert 0.90 <= f < 1.0                                  # a real, sub-1 adjustment
    # avg_price is a real cached number, per-type
    assert avg_price("hackney", "flat-maisonette", "2024-06", offline=True) > 300000


def test_factor_noops_safely():
    # at/after as-of -> nothing to lift
    assert hpi_factor("hackney", "flat", AS_OF_MONTH + "-01", offline=True) == 1.0
    # future-dated comp (no data) -> no adjustment, never a guess
    assert hpi_factor("hackney", "flat", "2026-02-01", offline=True) == 1.0
    # unmapped / uncached region offline -> no data -> 1.0
    assert hpi_factor("nowhere-region", "flat", "2021-01-01", offline=True) == 1.0


def test_factor_is_clamped():
    # even if data were extreme, the factor never leaves the sane band
    f = hpi_factor("hackney", "flat-maisonette", "2021-01-01", offline=True)
    assert 0.5 <= f <= 2.0


# ---------------------------------------------------------------------------
# Plain-stdlib runner.
# ---------------------------------------------------------------------------

def test_cache_schema_mismatch_is_a_miss():
    """A future-shaped cached month reads as a miss (None offline); a
    version-less one is valid v1."""
    import json as _json
    import tempfile
    import gaff_engine.hpi as H

    tmp = tempfile.mkdtemp()
    old_user, old_ship = H.CACHE_DIR, H.SHIPPED_CACHE_DIR
    H.CACHE_DIR = H.SHIPPED_CACHE_DIR = tmp
    try:
        p = H._cache_path("testshire", "2024-01")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        _json.dump({"cacheSchema": 999, "averagePriceFlatMaisonette": 100000}, open(p, "w"))
        assert H.avg_price("testshire", "flat-maisonette", "2024-01", offline=True) is None
        _json.dump({"averagePriceFlatMaisonette": 100000}, open(p, "w"))
        assert H.avg_price("testshire", "flat-maisonette", "2024-01", offline=True) == 100000
    finally:
        H.CACHE_DIR, H.SHIPPED_CACHE_DIR = old_user, old_ship


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
