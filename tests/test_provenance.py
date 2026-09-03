"""P9 provenance tests — show your work + the trust spine (09-data-trust.md §5.4/§5.5).

The truth half of the pitch only counts if every number is traceable:
- **A (SourceLabel).** `source_label` resolves a source (registry id OR the engine's
  human string) to a licence attribution rendered VERBATIM from `source.registry@1`,
  with the year filled — and `hm_land_registry` and `land_registry` resolve to one label.
- **The null rule.** A missing datum renders "not available from {source}", never a blank.
- **attribute_score.** Every number in the golden score traces to a source with a
  non-empty attribution; the Value Verdict cites its comp count inline (the truth
  centrepiece has to look sourced).
- **GDPR.** Export never includes shared property data; the delete plan never touches
  Listings/comps/forensics and transfers (not destroys) a shared Search.

    python3 -m pytest tests/test_provenance.py -v
    python3 tests/test_provenance.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.engine import score  # noqa: E402
from gaff_engine.elicit import person_from_profile  # noqa: E402
from gaff_engine.taste import canonical_model  # noqa: E402
from gaff_engine.forensics import canonical_model as canonical_forensics  # noqa: E402
from gaff_engine.provenance import (  # noqa: E402
    SOURCE_REGISTRY, attribute_score, basemap_credit, consent_record, deletion_plan,
    export_bundle, not_available, persona_badge, source_label,
)
from gaff_engine.serialize import to_jsonable  # noqa: E402
from gaff_engine.schemas import Ref  # noqa: E402
from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_SEARCH  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PERSON = person_from_profile(json.load(open(os.path.join(HERE, "profile.json"))))
_SCORE = score(GOLDEN_LISTING, _PERSON, GOLDEN_SEARCH,
               taste_model=canonical_model(), forensics_model=canonical_forensics())
PREF = Ref(id="person_finn", schemaVersion="person@1")


# ---------------------------------------------------------------------------
# §5.5a / A — SourceLabel: verbatim licence attribution.
# ---------------------------------------------------------------------------

def test_source_label_renders_the_ogl_attribution_verbatim():
    sl = source_label("HM Land Registry Price Paid", source_date="2026-06-30")
    assert validate(sl) == [], validate(sl)
    assert sl.source == "land_registry" and sl.label == "HM Land Registry Price Paid"
    assert "Open Government Licence v3.0" in sl.attribution
    assert "2026" in sl.attribution and "{year}" not in sl.attribution     # the year is filled


def test_hm_land_registry_and_land_registry_resolve_to_one_label():
    assert source_label("hm_land_registry").source == "land_registry"
    assert source_label("land_registry").label == "HM Land Registry Price Paid"


def test_every_ogl_source_has_a_nonempty_attribution():
    for sid, e in SOURCE_REGISTRY.items():
        assert e.attribution, sid
        # licence-bearing external sources must carry a real attribution
        if e.licence.startswith("Open Government Licence") or e.licence == "ODbL":
            assert "©" in e.attribution, sid


def test_null_datum_names_its_source_not_a_blank():
    assert not_available("environment_agency") == "not available from Environment Agency flood risk"
    assert basemap_credit() == "© OpenStreetMap contributors"


# ---------------------------------------------------------------------------
# §5.5 — attribute_score: every number → its source.
# ---------------------------------------------------------------------------

def test_attribute_score_traces_every_number():
    rows = attribute_score(_SCORE, GOLDEN_LISTING)
    assert all(validate(r) == [] for r in rows)
    by = {r.claim: r for r in rows}
    # the Value Verdict → HM Land Registry, with the comp count inline (rule 4)
    vv = by["Value verdict"]
    assert vv.sourceLabels[0].source == "land_registry" and vv.sourceLabels[0].attribution
    assert "comps" in vv.note and "/sqft" in vv.note
    # taste is honestly a model read, not an external fact
    assert by["Taste fit"].sourceLabels[0].source == "taste_model"
    # the listing facts cite the listing
    assert by["The listing"].sourceLabels[0].source == "listing"
    # every rendered source label carries a label (no blanks)
    for r in rows:
        for sl in r.sourceLabels:
            assert sl.label


def test_flood_absent_renders_the_null_rule():
    rows = {r.claim: r for r in attribute_score(_SCORE, GOLDEN_LISTING)}
    flood = rows["Flood risk"]
    # the golden carries no flood flag → the null rule, never a blank, and freshness "miss"
    assert flood.value == "not available from Environment Agency flood risk"
    assert flood.sourceLabels[0].freshness == "miss"


# ---------------------------------------------------------------------------
# §5.5b — the demo-vs-real persona badge (two axes).
# ---------------------------------------------------------------------------

def test_persona_badge_labels_both_axes():
    real = persona_badge(GOLDEN_SEARCH, GOLDEN_LISTING)
    assert real["listingReal"] is True and real["personReal"] is True
    assert real["label"] == "Real listing · your profile"
    demo_person = persona_badge(GOLDEN_SEARCH, GOLDEN_LISTING, person_is_demo=True)
    assert demo_person["label"] == "Demo profile · real listings" and demo_person["listingReal"] is True


# ---------------------------------------------------------------------------
# §5.4 — the GDPR spine.
# ---------------------------------------------------------------------------

def test_consent_record_has_contract_and_optional_bases():
    c = consent_record(PREF)
    assert validate(c) == [], validate(c)
    assert c.purposes["core_profiling"]["basis"] == "contract" and c.purposes["core_profiling"]["granted"] is True
    assert c.purposes["taste_twin_contribution"]["revocable"] is True
    assert c.purposes["product_analytics"]["granted"] is False     # off by default


def test_export_bundle_excludes_shared_property_data():
    c = to_jsonable(consent_record(PREF))
    b = export_bundle({"id": "person_finn"}, [{"id": "search_e8"}], [{"id": "fb_1"}], c)
    assert validate(b) == [], validate(b)
    joined = " ".join(b.notIncluded).lower()
    assert "property data" in joined and "other persons" in joined
    assert b.person["id"] == "person_finn" and b.feedback[0]["id"] == "fb_1"


def test_deletion_plan_never_touches_property_data():
    plan = deletion_plan(PREF, owned_searches=["s1", "s2"], shared_as_owner=["s3"],
                         shared_as_collaborator=["s4"], feedback_count=42)
    untouched = " ".join(plan["untouched"]).lower()
    assert "listings" in untouched and "comps" in untouched and "forensics" in untouched
    # a shared Search owned by the deleted Person transfers, not destroyed
    assert "transfersNotDestroyed" in plan and "longest-standing editor" in plan["transfersNotDestroyed"][0]
    # the tombstone holds no personal data
    assert plan["tombstone"]["personalData"] is None and plan["idempotent"] is True


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
