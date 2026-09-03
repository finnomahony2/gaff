"""T10/T8 tests — the productised repeat-sales pipeline (gaff_engine.flips).

DETERMINISTIC + OFFLINE: pairing and summarising are pure; the HPI adjustment
runs offline against the shipped month cache; the network stage is exercised
with a monkeypatched urlopen. The shipped Leamington dataset doubles as the
integration fixture.

    python3 -m pytest tests/test_flips.py -v
    python3 tests/test_flips.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import flips  # noqa: E402


def _sale(price, date, paon="10", street="SAMPLE ROAD", postcode="CV1 1AA",
          district="COVENTRY", ptype="terraced", saon=None):
    return {"price": price, "date": date, "paon": paon, "saon": saon,
            "street": street, "postcode": postcode, "town": "COVENTRY",
            "district": district, "type": ptype}


# ---------------------------------------------------------------------------
# 1 · Dates: both upstream formats parse; garbage refuses.
# ---------------------------------------------------------------------------

def test_dates_parse_both_formats_and_refuse_garbage():
    assert flips.parse_ppd_date("Fri, 20 Feb 2026").month == 2
    assert flips.parse_ppd_date("2026-02-20").day == 20
    assert flips.parse_ppd_date("2026-02").day == 1
    try:
        flips.parse_ppd_date("soonish")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# 2 · Pairing: the flip window, address identity, poison handling.
# ---------------------------------------------------------------------------

def test_pairing_respects_the_flip_window():
    rows = [_sale(200000, "2018-01-01"), _sale(250000, "2020-06-01"),   # 2.4y ✓
            _sale(300000, "2021-01-01")]                                # 0.6y ✗
    pairs = flips.pair_repeat_sales(rows)
    assert len(pairs) == 1
    assert pairs[0]["a"]["price"] == 200000 and pairs[0]["b"]["price"] == 250000


def test_pairing_needs_the_same_full_address():
    rows = [_sale(200000, "2018-01-01", saon="FLAT 1"),
            _sale(260000, "2020-06-01", saon="FLAT 2")]      # different flats
    assert flips.pair_repeat_sales(rows) == []


def test_pairing_skips_rows_missing_essentials_and_bad_dates():
    rows = [_sale(None, "2018-01-01"), _sale(250000, "2020-06-01"),
            _sale(200000, "not a date", paon="11"), _sale(250000, "2020-06-01", paon="11")]
    assert flips.pair_repeat_sales(rows) == []


# ---------------------------------------------------------------------------
# 3 · HPI adjustment: fail-closed regions, artefact band, real cached months.
# ---------------------------------------------------------------------------

def test_flip_records_offline_against_the_shipped_hpi_cache():
    """A Hackney pair over cached months gets a real market comparison."""
    rows = [_sale(500000, "2021-06-15", postcode="E8 1AA", district="HACKNEY",
                  ptype="flat-maisonette"),
            _sale(600000, "2024-06-15", postcode="E8 1AA", district="HACKNEY",
                  ptype="flat-maisonette")]
    recs = flips.flip_records(flips.pair_repeat_sales(rows), offline=True)
    assert len(recs) == 1
    r = recs[0]
    assert r["uplift_pct"] == 20.0
    assert r["market_pct"] is not None                 # from the shipped cache
    assert abs((1 + r["uplift_pct"] / 100) / (1 + r["market_pct"] / 100)
               - (1 + r["excess_pct"] / 100)) < 0.01   # excess recomputes


def test_unknown_region_is_skipped_never_londonised():
    """The old region fallback priced any unknown district against the London
    series. A district with no cached HPI must yield NO record."""
    rows = [_sale(200000, "2021-06-15", district="NOWHERESHIRE"),
            _sale(260000, "2023-06-15", district="NOWHERESHIRE")]
    assert flips.flip_records(flips.pair_repeat_sales(rows), offline=True) == []


def test_artefact_band_filters_plot_splits():
    rows = [_sale(100000, "2021-06-15", postcode="E8 1AA", district="HACKNEY",
                  ptype="flat-maisonette"),
            _sale(500000, "2024-06-15", postcode="E8 1AA", district="HACKNEY",
                  ptype="flat-maisonette")]                     # +400%: artefact
    assert flips.flip_records(flips.pair_repeat_sales(rows), offline=True) == []


# ---------------------------------------------------------------------------
# 4 · T8 — the scale guard refuses before downloading.
# ---------------------------------------------------------------------------

def _fake_urlopen(pages_with_items):
    import io as _io

    def fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        import urllib.parse as up
        page = int(up.parse_qs(up.urlparse(url).query).get("_page", ["0"])[0])
        items = [{"pricePaid": 1, "transactionDate": "2020-01-01",
                  "propertyAddress": {}}] if page in pages_with_items else []
        body = json.dumps({"result": {"items": items}}).encode()
        return _io.BytesIO(body)
    return fake


def test_oversized_town_is_refused_after_one_probe():
    import urllib.request
    flips.REQUEST_PACING_SECONDS, pace = 0, flips.REQUEST_PACING_SECONDS
    real = urllib.request.urlopen
    calls = []
    fake = _fake_urlopen(set(range(0, 1000)))          # every page has items

    def counting(req, timeout=None):
        import urllib.parse as up
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(int(up.parse_qs(up.urlparse(url).query)["_page"][0]))
        return fake(req, timeout)
    urllib.request.urlopen = counting
    try:
        try:
            flips.pull_town("MEGACITY", max_records=1000)
            raise AssertionError("expected TownTooLargeError")
        except flips.TownTooLargeError as e:
            assert "MEGACITY" in str(e) and "1000" in str(e)
        assert calls == [5]                # ONE probe, AT the cap page (1000/200)
    finally:
        urllib.request.urlopen = real
        flips.REQUEST_PACING_SECONDS = pace


def test_small_town_pulls_completely():
    import urllib.request
    flips.REQUEST_PACING_SECONDS, pace = 0, flips.REQUEST_PACING_SECONDS
    real = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen({0, 1})      # 2 pages then empty
    try:
        rows = flips.pull_town("TINYTOWN", max_records=1000)
        assert len(rows) == 2                           # 1 item per fake page
    finally:
        urllib.request.urlopen = real
        flips.REQUEST_PACING_SECONDS = pace


# ---------------------------------------------------------------------------
# 4b · Review regressions.
# ---------------------------------------------------------------------------

def test_new_build_buys_never_anchor_a_pair():
    """The research excluded developer first sales; the port must too."""
    rows = [dict(_sale(300000, "2018-01-01"), newBuild=True),
            _sale(380000, "2020-06-01")]
    assert flips.pair_repeat_sales(rows) == []
    # ...but a new-build SELL of a later genuine resale still pairs
    rows2 = [_sale(300000, "2018-01-01"), dict(_sale(380000, "2020-06-01"), newBuild=True)]
    assert len(flips.pair_repeat_sales(rows2)) == 1


def test_regenerated_town_shadows_instead_of_doubling():
    """Records dedupe by identity, not filename: a rebuilt Leamington beside
    the shipped four-town file must not double-count 1,694 resales."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    old_env = os.environ.get("GAFF_CACHE_DIR")
    os.environ["GAFF_CACHE_DIR"] = tmp
    try:
        baseline = flips.summarise(flips.load_flips(), "LEAMINGTON SPA")["resales_analysed"]
        shipped = [r for r in flips.load_flips()
                   if (r.get("town") or "").upper() == "LEAMINGTON SPA"]
        os.makedirs(os.path.join(tmp, "flips"), exist_ok=True)
        with open(os.path.join(tmp, "flips", "leamington-spa.json"), "w") as fh:
            json.dump(shipped, fh)
        again = flips.summarise(flips.load_flips(), "LEAMINGTON SPA")["resales_analysed"]
        assert again == baseline, "regeneration doubled: %s -> %s" % (baseline, again)
    finally:
        if old_env is None:
            os.environ.pop("GAFF_CACHE_DIR", None)
        else:
            os.environ["GAFF_CACHE_DIR"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


def test_non_multiple_cap_does_not_falsely_refuse():
    """cap 1100 on a 1000-record town: probe page ceil(1100/200)=6 is empty,
    so the pull proceeds (the old floor probe hit page 5 and refused)."""
    import urllib.request
    flips.REQUEST_PACING_SECONDS, pace = 0, flips.REQUEST_PACING_SECONDS
    real = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(set(range(0, 5)))   # 5 pages = 5 rows
    try:
        rows = flips.pull_town("ODDCAP", max_records=1100)
        assert len(rows) == 5
    finally:
        urllib.request.urlopen = real
        flips.REQUEST_PACING_SECONDS = pace


def test_upstream_failure_is_a_named_contextual_error():
    import urllib.request
    flips.REQUEST_PACING_SECONDS, pace = 0, flips.REQUEST_PACING_SECONDS
    real = urllib.request.urlopen
    def boom(req, timeout=None):
        import urllib.error
        raise urllib.error.URLError("connection refused")
    urllib.request.urlopen = boom
    try:
        try:
            flips.pull_town("DEADTOWN", max_records=1000)
            raise AssertionError("expected FlipsFetchError")
        except flips.FlipsFetchError as e:
            assert "DEADTOWN" in str(e) and "URLError" in str(e)
    finally:
        urllib.request.urlopen = real
        flips.REQUEST_PACING_SECONDS = pace


# ---------------------------------------------------------------------------
# 5 · Integration: the shipped Leamington dataset through the engine.
# ---------------------------------------------------------------------------

def test_shipped_dataset_summarises_identically_to_the_surface():
    records = flips.load_flips()
    assert len(records) > 3000
    s = flips.summarise(records, "LEAMINGTON SPA")
    assert s["resales_analysed"] > 1000
    assert s["median_excess_over_market_pct"] is not None
    assert "Open Government Licence" in s["source"]
    # and the tool layer returns the engine's shape verbatim. The surfaces
    # live in spike/ in the lab and surfaces/ in the assembled package.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for d in ("spike", "surfaces"):
        if os.path.isdir(os.path.join(root, d)):
            sys.path.insert(0, os.path.join(root, d))
            break
    import gaff_tools
    assert gaff_tools.flip_stats("LEAMINGTON SPA") == s


def test_unknown_town_names_what_is_available():
    s = flips.summarise(flips.load_flips(), "ATLANTIS")
    assert "error" in s and "LEAMINGTON SPA" in s["towns_available"]


# ---------------------------------------------------------------------------
# 6 · Window sensitivity — parameterised pairing + per-window stability.
# ---------------------------------------------------------------------------

def test_pairing_window_is_a_parameter_with_unchanged_default():
    rows = [_sale(200000, "2018-01-01"), _sale(250000, "2018-09-01")]   # 0.67y gap
    assert flips.pair_repeat_sales(rows) == []                          # default refuses
    wide = flips.pair_repeat_sales(rows, min_gap_years=0.5, max_gap_years=5.5)
    assert len(wide) == 1
    long_rows = [_sale(200000, "2015-01-01"), _sale(300000, "2022-01-01")]  # 7y gap
    assert flips.pair_repeat_sales(long_rows) == []
    assert len(flips.pair_repeat_sales(long_rows, min_gap_years=1.5,
                                       max_gap_years=10)) == 1
    try:
        flips.pair_repeat_sales(rows, min_gap_years=5, max_gap_years=2)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def _rec(yrs, uplift, excess):
    return {"yrs": yrs, "uplift_pct": uplift, "excess_pct": excess,
            "market_pct": uplift - excess, "town": "TESTTOWN"}


def test_sensitivity_per_window_medians():
    records = [_rec(2.0, 10.0, 4.0), _rec(2.5, 12.0, 6.0),   # window (1,3)
               _rec(4.0, 15.0, 5.0), _rec(5.0, 16.0, 3.0)]   # window (3,7)
    s = flips.sensitivity(records, windows=[(1, 3), (3, 7)], min_n=1)
    w1, w2 = s["windows"]
    assert (w1["n"], w1["median_uplift_pct"], w1["median_excess_over_market_pct"]) == (2, 11.0, 5.0)
    assert (w2["n"], w2["median_excess_over_market_pct"]) == (2, 4.0)
    assert s["stable"] is True and not any("UNSTABLE" in n for n in s["notes"])


def test_sensitivity_flags_a_sign_flip():
    records = [_rec(2.0, 10.0, 3.0), _rec(2.5, 11.0, 4.0),
               _rec(5.0, 5.0, -2.0), _rec(6.0, 4.0, -3.0)]
    s = flips.sensitivity(records, windows=[(1, 3), (3, 7)], min_n=1)
    assert s["stable"] is False
    assert any("flips sign" in n for n in s["notes"])


def test_sensitivity_flags_excess_attenuating_to_noise():
    """A clear excess collapsing toward zero at longer holds is material —
    the noise floor must not hide a 10x attenuation."""
    records = [_rec(2.0, 10.0, 4.0), _rec(2.5, 11.0, 4.0),
               _rec(5.0, 8.0, 0.3), _rec(6.0, 9.0, 0.5)]
    s = flips.sensitivity(records, windows=[(1, 3), (3, 7)], min_n=1)
    assert s["stable"] is False
    assert any("magnitude" in n for n in s["notes"])


def test_sensitivity_small_windows_are_excluded_but_named():
    records = [_rec(2.0, 10.0, 4.0), _rec(2.5, 11.0, 5.0),
               _rec(6.0, 4.0, -9.0)]                       # sign-flip bait, but n=1
    s = flips.sensitivity(records, windows=[(1, 3), (3, 7)], min_n=2)
    assert s["stable"] is True                             # n=1 window can't testify
    assert any("n < 2" in n for n in s["notes"])


def test_sensitivity_notes_any_truncated_window_with_bounds_and_remedy():
    """A quarter-missing window (observed 1.5-3 inside a requested 1-3) used
    to pass the old half-window threshold silently; the note must now fire,
    name observed vs requested bounds, and say the re-pair needs RAW rows
    (the flips cache holds already-paired records, not PPD rows)."""
    records = [_rec(1.5, 10.0, 4.0), _rec(2.0, 11.0, 5.0), _rec(3.0, 12.0, 4.0)]
    s = flips.sensitivity(records, windows=[(1, 3)], min_n=1)
    note = next(n for n in s["notes"] if "does not cover" in n)
    assert "1-3yr" in note and "1.5-3" in note             # requested vs observed
    assert "RAW PPD rows" in note and "pull_town" in note  # honest remedy


def test_sensitivity_full_coverage_gets_no_truncation_note():
    """Edge slack: records within _TRUNCATION_TOLERANCE_YRS of both bounds
    count as covering the window — boundary sparsity is not truncation."""
    records = [_rec(1.1, 10.0, 4.0), _rec(2.0, 11.0, 5.0), _rec(2.9, 12.0, 4.0)]
    s = flips.sensitivity(records, windows=[(1, 3)], min_n=1)
    assert not any("does not cover" in n for n in s["notes"])


def test_sensitivity_accepts_raw_pairs_via_the_offline_hpi_cache():
    rows = [_sale(500000, "2021-06-15", postcode="E8 1AA", district="HACKNEY",
                  ptype="flat-maisonette"),
            _sale(600000, "2024-06-15", postcode="E8 1AA", district="HACKNEY",
                  ptype="flat-maisonette")]
    pairs = flips.pair_repeat_sales(rows)
    s = flips.sensitivity(pairs, windows=[(1, 5)], min_n=1)
    assert s["windows"][0]["n"] == 1
    assert s["windows"][0]["median_uplift_pct"] == 20.0


def test_sensitivity_on_the_shipped_leamington_records():
    """The review's ask: rerun over 1-3 / 3-7 / 5-10 on the warmed town. The
    shipped pairing spans 1.5-5.5yrs, so the 5-10 window is truncated — the
    result must SAY so rather than present 5-5.5 as 5-10."""
    records = [r for r in flips.load_flips()
               if (r.get("town") or "").upper() == "LEAMINGTON SPA"]
    s = flips.sensitivity(records)
    w13, w37, w510 = s["windows"]
    assert w13["n"] > 100 and w37["n"] > 100
    assert w37["yrs_observed"][1] <= flips.MAX_GAP_YEARS
    assert any("does not cover" in n and "5-10yr" in n for n in s["notes"])
    # The 1-3 window is 25% truncated too (the pairing's 1.5yr floor): its
    # "+4.0pp" headline is really a 1.5-3yr number and must say so.
    assert w13["yrs_observed"][0] >= flips.MIN_GAP_YEARS
    assert any("does not cover" in n and "1-3yr" in n for n in s["notes"])
    # excess attenuates ~4.0 → ~2.1 → ~0.4pp across the windows: the harness
    # must surface that as instability, not average it away.
    assert s["stable"] is False
    assert any("UNSTABLE" in n for n in s["notes"])



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
