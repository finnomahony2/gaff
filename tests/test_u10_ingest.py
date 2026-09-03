"""U10 tests — the ingestion path (09-data-trust §5.1): raw payload → listing@1.

DETERMINISTIC + OFFLINE: reads synthetic payloads from ``tests/fixtures/raw/``
(no network). They are authored to the portal's response *shape* — the field
mapping is what is under test — so this suite carries no scraped content and
runs anywhere. ``normalise`` takes an explicit ``today`` so the days-on-market
delta is reproducible.

The same parser is exercised against the real scraped corpus by
``tests/test_u10_corpus.py``, which is lab-only and needs ``data/raw/``.

    python3 -m pytest tests/test_u10_ingest.py -v
    python3 tests/test_u10_ingest.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.ingest import (  # noqa: E402
    dedupe, ingest_event, listing_key, normalise, parse_money,
)
from gaff_engine.schemas import Listing, Mode, MoneyPeriod  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "raw")


def _load(name):
    with open(os.path.join(_FIXTURES, name)) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1 · normalise a known real payload — the field mapping (§5.1c).
# ---------------------------------------------------------------------------

def test_normalise_maps_the_documented_fields():
    L = normalise(_load("sample_let.json"), today="2026-07-14")
    assert isinstance(L, Listing)
    assert L.address.display == "Sample Street, Exampleton, N1"     # \n stripped
    assert L.address.postcode == "N1 0QY"                           # outcode + incode
    assert L.beds == 4 and L.baths == 2
    assert L.geo and L.geo.accuracy == "accurate"                  # ACCURATE_POINT
    assert L.sqft and L.sqft > 0                                    # derived from sqm
    assert L.nearestStations and L.nearestStations[0].distanceMiles is not None
    assert L.agent and L.agent.companyName                          # from raw.customer
    assert validate(L) == []                                        # contract-clean


def test_description_extracts_the_prose_not_the_whole_text_block():
    """Rightmove's `text` field is a dict of description/disclaimer/share-copy
    variants; a naive str(raw["text"]) stringifies the whole dict into a
    Python-repr mess instead of the actual prose."""
    L = normalise(_load("sample_buy.json"), today="2026-07-14")
    assert L.description.startswith("An attractive and versatile two bedroom")
    assert "Sample Estate" in L.description
    assert "{'description'" not in L.description and "propertyPhrase" not in L.description


def test_sqft_prefers_stated_else_derives_from_sqm():
    from gaff_engine.ingest import _sqft
    # a stated sqft is used verbatim.
    assert _sqft([{"unit": "sqft", "maximumSize": 1844}]) == (1844, "stated")
    # sqm-only → derived (and marked), 100 m² ≈ 1076 sqft.
    val, note = _sqft([{"unit": "sqm", "maximumSize": 100}])
    assert note == "derived" and 1070 <= val <= 1080
    # a payload that derives records the honest completeness note.
    L = normalise({"id": "x", "channel": "RES_LET", "sizings": [{"unit": "sqm", "maximumSize": 100}],
                   "address": {"displayAddress": "A St, N1", "outcode": "N1", "incode": "1AA"}},
                  today="2026-07-14")
    assert (L.provenance.completeness or {}).get("sqft") == "derived"


def test_money_parser():
    m, note = parse_money("£8,500 pcm")
    assert m.amount == 8500 and m.period == MoneyPeriod.PCM and note is None
    assert parse_money("£1,150,000")[0].period == MoneyPeriod.TOTAL
    assert parse_money("Guide Price £1,150,000")[0].amount == 1150000
    assert parse_money(None) == (None, "missing")


def test_money_parser_units_abutting_the_digits():
    """BACKLOG 1b P1, pinned: the amount regex's trailing \\b backtracked to
    the comma when a period token abutted the digits, so "£2,000pcm" parsed
    as £2 — confidently wrong, the exact class the T4 amendment was meant to
    end. The amount must parse fully AND the period must still be read."""
    m, note = parse_money("£2,000pcm")
    assert m.amount == 2000 and m.period == MoneyPeriod.PCM and note is None
    m, _ = parse_money("£450pw")
    assert m.amount == 450 and m.period == MoneyPeriod.PW
    m, _ = parse_money("£2,000pm")
    assert m.amount == 2000 and m.period == MoneyPeriod.PCM


def test_money_parser_suffixes_survive_trailing_punctuation():
    """The m/k expansion must not regress while fixing the abutting-unit bug:
    the suffix's own lookahead replaces the old \\b, and punctuation after
    the suffix is not a word continuation."""
    assert parse_money("£1.2m,")[0].amount == 1_200_000
    assert parse_money("£450k.")[0].amount == 450_000
    # "development" must still never read as pm (the original substring bug).
    assert parse_money("£500,000 development")[0].period == MoneyPeriod.TOTAL


# ---------------------------------------------------------------------------
# 3 · listingKey + dedupe — the idempotent inbound contract (§5.1d).
# ---------------------------------------------------------------------------

def test_listing_key_is_stable_and_deterministic():
    a = normalise(_load("sample_let.json"), today="2026-07-14")
    b = normalise(_load("sample_let.json"), today="2026-07-14")
    assert listing_key(a) == listing_key(b)                         # same raw → same key
    assert len(listing_key(a)) == 40                                # sha1 hex


def test_dedupe_hit_miss_merge():
    L = normalise(_load("sample_let.json"), today="2026-07-14")
    key = listing_key(L)
    empty = {}
    assert dedupe(L, empty) == "new_listing"                        # key miss
    seen_same = {key: [p.id for p in L.portalIds]}
    assert dedupe(L, seen_same) == "duplicate_ignored"              # re-forward, same portal
    seen_other = {key: ["99999999"]}
    assert dedupe(L, seen_other) == "merged_into_existing"          # same home, new portal id


def test_ingest_event_envelope():
    L = normalise(_load("sample_let.json"), today="2026-07-14")
    ev = ingest_event(L, raw_body="<html>...</html>")
    assert ev.schemaVersion == "ingest.event@1"
    assert ev.state.value == "normalised" and ev.dedupeKey.startswith("sha1:")
    assert ev.listingKey == listing_key(L)


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
