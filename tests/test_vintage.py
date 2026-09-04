"""S5 tests — evidence vintage: how old the evidence under a verdict really is.

    python3 tests/test_vintage.py
    python3 -m pytest tests/test_vintage.py -v     # if pytest is installed

The gap this closes is assessment finding F6: price_check returned fetchedAt,
value_check and score_listing returned nothing, and staleness that is not printed
looks like precision.

Every date-arithmetic test passes ``today=`` explicitly. The wall clock is a real
input to staleness — "stale" is a fact about the moment a decision is made, not
about a file — but a test that reads it is a test whose result changes overnight.
"""

import datetime
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine import paths, tools, vintage  # noqa: E402

TODAY = datetime.date(2026, 9, 3)


def _comp(date):
    class _C:
        pass
    c = _C()
    c.date = date
    return c


# ---------------------------------------------------------------------------
# The struct.
# ---------------------------------------------------------------------------

def test_the_four_facts():
    v = vintage.evidence_vintage(
        [_comp("2026-04-28"), _comp("2025-11-02"), _comp("2026-01-14")],
        sources=[{"name": "london sales cache", "fetchedAt": "2026-07-16T09:00:00Z"}],
        hpi_month="2025-06", hpi_region="hackney", today=TODAY)
    assert v["newestSale"] == "2026-04-28"          # 1. the freshest sale
    assert v["oldestSale"] == "2025-11-02"
    assert v["fetchedAt"] == "2026-07-16"           # 2. when it was pulled
    assert v["hpiMonth"] == "2025-06"               # 3. the month adjusted to
    assert v["hpiRegion"] == "hackney"
    assert v["stale"] is True                       # 4. the staleness line
    assert "over 90 days" in v["line"]


def test_the_two_ages_answer_different_questions():
    """publicationLagDays is a property of the data and reproducible;
    evidenceAgeDays is how old it is right now."""
    v = vintage.evidence_vintage(
        [_comp("2026-05-29")], sources=[{"name": "c", "fetchedAt": "2026-07-16"}],
        today=TODAY)
    assert v["publicationLagDays"] == 48            # 29 May -> 16 Jul
    assert v["evidenceAgeDays"] == 97               # 29 May -> 3 Sep
    # The lag does not move when the clock does; the age does.
    later = vintage.evidence_vintage(
        [_comp("2026-05-29")], sources=[{"name": "c", "fetchedAt": "2026-07-16"}],
        today=datetime.date(2026, 12, 25))
    assert later["publicationLagDays"] == 48
    assert later["evidenceAgeDays"] == 210


def test_the_threshold_is_a_boundary_not_a_vibe():
    newest = TODAY - datetime.timedelta(days=vintage.STALE_DAYS)
    at = vintage.evidence_vintage([_comp(newest.isoformat())], today=TODAY)
    assert at["evidenceAgeDays"] == 90 and at["stale"] is False
    over = vintage.evidence_vintage(
        [_comp((newest - datetime.timedelta(days=1)).isoformat())], today=TODAY)
    assert over["evidenceAgeDays"] == 91 and over["stale"] is True


def test_fresh_evidence_says_nothing_about_staleness():
    v = vintage.evidence_vintage(
        [_comp("2026-08-20")], sources=[{"name": "c", "fetchedAt": "2026-08-29"}],
        hpi_month="2026-07", today=TODAY)
    assert v["stale"] is False
    assert "over 90 days" not in v["line"]
    assert "20 Aug 2026" in v["line"] and "Jul 2026" in v["line"]


def test_absence_is_reported_as_absence_never_guessed():
    empty = vintage.evidence_vintage([], today=TODAY)
    assert empty["newestSale"] is None and empty["stale"] is False
    assert "no comparable sales" in empty["line"].lower()

    undated = vintage.evidence_vintage([_comp(None), _comp("")], today=TODAY)
    assert undated["newestSale"] is None and undated["datedComps"] == 0
    assert "undated" in undated["line"]

    # A partly dated pool prices on what it has and says how much it lacks.
    mixed = vintage.evidence_vintage([_comp("2026-04-28"), _comp(None)], today=TODAY)
    assert mixed["datedComps"] == 1 and mixed["comps"] == 2
    assert "1 of 2 comparables carry no date" in mixed["line"]

    # No fetch stamp: the sale date still stands on its own.
    nofetch = vintage.evidence_vintage([_comp("2026-04-28")], today=TODAY)
    assert nofetch["fetchedAt"] is None and nofetch["publicationLagDays"] is None
    assert "Fetched" not in nofetch["line"]


def test_a_pool_is_no_fresher_than_its_stalest_ingredient():
    """The oldest fetch wins, not the newest. Taking the newest reported the
    shipped London pool as pulled on 29 August when its sales were pulled in
    mid-July — six weeks of invented freshness on the one number that exists to
    prevent exactly that."""
    v = vintage.evidence_vintage(
        [_comp("2026-04-28")],
        sources=[{"name": "a", "fetchedAt": "2026-08-29T12:31:28Z"},
                 {"name": "b", "fetchedAt": "2026-07-16T09:00:00Z"},
                 {"name": "c", "fetchedAt": None}],
        today=TODAY)
    assert v["fetchedAt"] == "2026-07-16"
    assert [s["name"] for s in v["sources"]] == ["a", "b"]     # the dated ones


def test_a_derivation_date_can_never_stand_in_for_a_fetch():
    """The enriched comparables file's generatedAt says when the ENRICHMENT ran
    and nothing about when the sales were pulled."""
    v = vintage.evidence_vintage(
        [_comp("2026-04-28")],
        sources=[{"name": "enriched", "fetchedAt": "2026-08-29", "kind": "derived"},
                 {"name": "sales cache", "fetchedAt": "2026-07-15", "kind": "fetch"}],
        today=TODAY)
    assert v["fetchedAt"] == "2026-07-15"
    assert v["derivedAt"] == "2026-08-29"
    assert "15 Jul 2026" in v["line"] and "29 Aug" not in v["line"]
    # A pool with ONLY a derived source has no honest fetch date at all.
    only = vintage.evidence_vintage(
        [_comp("2026-04-28")],
        sources=[{"name": "enriched", "fetchedAt": "2026-08-29", "kind": "derived"}],
        today=TODAY)
    assert only["fetchedAt"] is None and only["derivedAt"] == "2026-08-29"


def test_dicts_and_objects_both_read():
    assert vintage.evidence_vintage([{"date": "2026-04-28"}],
                                    today=TODAY)["newestSale"] == "2026-04-28"
    assert vintage.evidence_vintage([{"soldDate": "2026-04-28"}],
                                    today=TODAY)["newestSale"] == "2026-04-28"
    assert vintage.evidence_vintage([{"date": "not a date"}],
                                    today=TODAY)["newestSale"] is None
    assert vintage.evidence_vintage([{"date": "2026-13-45"}],
                                    today=TODAY)["newestSale"] is None


def test_the_line_is_the_one_field_a_surface_quotes():
    """The point of the single field: a surface should never have to assemble
    five numbers into a sentence and word it differently each time."""
    v = vintage.evidence_vintage(
        [_comp("2026-04-28")], sources=[{"name": "c", "fetchedAt": "2026-07-16"}],
        hpi_month="2025-06", hpi_region="hackney", today=TODAY)
    for fragment in ("28 Apr 2026", "16 Jul 2026", "Jun 2025", "hackney", "days"):
        assert fragment in v["line"], fragment


def test_the_line_names_the_region_only_when_one_was_actually_used():
    """A verdict with no HPI region was NOT time-adjusted, and the line has to
    say so rather than naming a month as though it had been.

    Before hpi.region_for learned to abstain, an unplaceable subject resolved to
    "london", so this line read "Prices adjusted to Jun 2025 money (london)" for
    a Leeds flat — an honest report of what the engine did, and a wrong
    adjustment. Now the region is None and the sentence changes shape.
    """
    adjusted = vintage.evidence_vintage(
        [_comp("2026-04-28")], hpi_month="2025-06", hpi_region="hackney", today=TODAY)
    assert adjusted["hpiAdjusted"] is True
    assert "adjusted to Jun 2025 money (hackney)" in adjusted["line"]
    assert "NOT adjusted" not in adjusted["line"]

    unplaced = vintage.evidence_vintage(
        [_comp("2026-04-28")], hpi_month="2025-06", hpi_region=None, today=TODAY)
    assert unplaced["hpiAdjusted"] is False
    assert unplaced["hpiRegion"] is None
    # It still names the month it would have used, so the reader knows what was
    # skipped — but it never claims the adjustment happened.
    assert "NOT adjusted to Jun 2025 money" in unplaced["line"]
    assert "money of its own date" in unplaced["line"]
    assert "(london)" not in unplaced["line"]


def test_a_known_region_with_no_data_is_still_reported_as_unadjusted():
    """The second reason nothing moved, and it needs its own sentence.

    A region can resolve perfectly well and still adjust nothing, when its HPI
    months are neither cached nor fetchable. Inferring "adjusted" from the
    region alone made the line claim an adjustment that did not happen — so the
    caller passes the outcome and the line names the real reason."""
    v = vintage.evidence_vintage(
        [_comp("2026-04-28")], hpi_month="2025-06", hpi_region="hackney",
        hpi_adjusted=False, today=TODAY)
    assert v["hpiAdjusted"] is False
    assert v["hpiRegion"] == "hackney"          # the region is still a true fact
    assert "NOT adjusted to Jun 2025 money" in v["line"]
    assert "no UK HPI data was available for hackney" in v["line"]
    # ...and it must NOT be confused with the unplaceable-subject reason.
    assert "not in the UK HPI region map" not in v["line"]


def test_the_basis_and_the_vintage_line_never_disagree():
    """One verdict, two surfaces, one story.

    The regression this pins: the vintage line used to be built from a region
    re-derived at the tool boundary, while the basis bit was built from whether
    a comp actually moved. With HPI unreachable the basis correctly said
    nothing and the line still read "Prices adjusted to Jun 2025 money
    (hackney)" — the precise overclaim S5 exists to remove."""
    from gaff_engine import hpi

    with _Isolated():
        real = hpi.fetch_month
        hpi.fetch_month = lambda *a, **k: None       # no cache, no network
        try:
            out = tools.value_check(fields=dict(LONDON))
        finally:
            hpi.fetch_month = real
        assert "time-adjusted" not in out["basis"], out["basis"]
        assert out["vintage"]["hpiAdjusted"] is False
        assert "NOT adjusted" in out["vintage"]["line"]

    with _Isolated():
        out = tools.value_check(fields=dict(LONDON))
        moved = "time-adjusted" in out["basis"]
        assert out["vintage"]["hpiAdjusted"] is moved
        if moved:
            assert "Prices adjusted to" in out["vintage"]["line"]


def test_an_unplaceable_subject_is_reported_as_unadjusted_end_to_end():
    """hpi.region_for -> vintage, joined up: the module that abstains and the
    line that reports it, over one subject the area map cannot place."""
    from gaff_engine import hpi

    leeds = {"address": "42 Kirkstall Road, Leeds LS3"}
    assert hpi.region_for(leeds) is None
    v = vintage.evidence_vintage([_comp("2026-04-28")], hpi_month=hpi.AS_OF_MONTH,
                                 hpi_region=hpi.region_for(leeds), today=TODAY)
    assert v["hpiAdjusted"] is False and "NOT adjusted" in v["line"]

    hackney = {"address": "12 De Beauvoir Road, London N1"}
    v2 = vintage.evidence_vintage([_comp("2026-04-28")], hpi_month=hpi.AS_OF_MONTH,
                                  hpi_region=hpi.region_for(hackney), today=TODAY)
    assert v2["hpiAdjusted"] is True and "(hackney)" in v2["line"]


# ---------------------------------------------------------------------------
# Threaded into the three tools (the S5 ask).
# ---------------------------------------------------------------------------

class _Isolated:
    """A temp user cache, so the tools read only the shipped warm data."""

    def __enter__(self):
        self._dir = tempfile.mkdtemp(prefix="gaff-vintage-test-")
        self._old = os.environ.get(paths.ENV_CACHE_DIR)
        os.environ[paths.ENV_CACHE_DIR] = self._dir
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop(paths.ENV_CACHE_DIR, None)
        else:
            os.environ[paths.ENV_CACHE_DIR] = self._old
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


LONDON = {"address": "De Beauvoir Road, London N1", "beds": 2, "baths": 2,
          "sqft": 1050, "price": "£1,250,000", "property_type": "maisonette"}
AXES = ["light_and_volume", "outdoor_space", "character_bones",
        "width_proportion_flow", "street_scene", "raw_size_threshold",
        "design_finish", "station_proximity"]
READS = {"axes": {a: {"score": 7.0, "contribution": "c"} for a in AXES}}


def _assert_real(v):
    assert v is not None, "no vintage on the payload"
    assert v["newestSale"], "the pool's freshest sale was not reported"
    assert v["fetchedAt"], "the cache's fetch stamp was not reported"
    assert v["hpiMonth"], "the HPI month the comps were adjusted to was not reported"
    assert v["line"]


def test_value_check_carries_the_vintage():
    with _Isolated():
        _assert_real(tools.value_check(fields=dict(LONDON)).get("vintage"))


def test_score_listing_carries_the_vintage():
    with _Isolated():
        out = tools.score_listing(fields=dict(LONDON), reads=READS)
        _assert_real((out.get("value") or {}).get("vintage"))
        assert out["workings"]["vintage"] is not None


def test_show_work_carries_and_narrates_the_vintage():
    with _Isolated():
        work = tools.show_work(fields=dict(LONDON))
        _assert_real(work.get("vintage"))
        assert "Evidence vintage" in work["rendered"]
        assert work["vintage"]["line"] in work["rendered"]


def test_a_stale_pool_says_so_in_the_narrative_a_reader_actually_sees():
    """Not only in the trace. The shipped London pool IS stale — its freshest
    sale is months old — so this is the real case, not a contrived one."""
    with _Isolated():
        out = tools.score_listing(fields=dict(LONDON), reads=READS)
        if out["value"]["vintage"]["stale"]:
            assert "Evidence age:" in out["narrative"]
        else:                       # a future rewarm makes the pool fresh again
            assert "Evidence age:" not in out["narrative"]


def test_a_refusal_carries_no_vintage_because_there_is_no_verdict():
    with _Isolated():
        out = tools.value_check(fields={"address": "Rue de Rivoli, Paris",
                                        "beds": 2, "sqft": 700, "price": "£300,000"})
        assert "error" in out
        assert "vintage" not in out


def test_the_hpi_pin_is_visible_beside_the_freshest_sale():
    """The reason M-2 comes before M-1. A verdict adjusted to a pinned month,
    with sales far newer than it, is measuring the pin as much as the market —
    and until S5 nothing said which month had been used."""
    with _Isolated():
        v = tools.value_check(fields=dict(LONDON))["vintage"]
        from gaff_engine import hpi
        assert v["hpiMonth"] == hpi.AS_OF_MONTH
        assert v["hpiMonth"] in v["line"] or "Jun 2025" in v["line"]


# ---------------------------------------------------------------------------
# Plain-stdlib runner (works without pytest) — mirrors tests/test_engine.py.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception as exc:                        # noqa: BLE001
                failures += 1
                import traceback
                traceback.print_exc()
                print("FAIL %s: %s" % (name, exc))
    print("\n%s" % ("all vintage tests passed" if not failures
                    else "%d FAILURES" % failures))
    sys.exit(1 if failures else 0)
