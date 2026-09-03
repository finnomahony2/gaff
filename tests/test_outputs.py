"""P8 outputs tests — the market report + the documents pack (08-action.md §5.2/§5.3).

- **A4 (every claim sourced).** `market_report` over the real Buy feed produces sections whose
  every stat carries a `sources[]`; `report_lint` passes it and rejects an unsourced stat.
- **A5 (data, not advice).** The report presents the market and issues no personalised buy/sell
  recommendation.
- **A7 (readiness from the item set).** `assemble_docpack` computes `requiredProvided/requiredTotal`
  — Finn's buy_mortgaged pack mid-assembly is 50% with exactly the three funds items missing.
- **A6 (never transacts, never autofills).** The pack holds vault *references* + Gaff-generated
  cover docs, never a third-party secret value; sharing is default-private; there is no submit.

    python3 -m pytest tests/test_outputs.py -v
    python3 tests/test_outputs.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.engine import score  # noqa: E402
from gaff_engine.elicit import person_from_profile  # noqa: E402
from gaff_engine.taste import canonical_model  # noqa: E402
from gaff_engine.forensics import canonical_model as canonical_forensics  # noqa: E402
from gaff_engine.outputs import (  # noqa: E402
    DOCPACK_CONFIG, assemble_docpack, holds_no_secret_values, market_report, report_lint,
)
from gaff_engine.fixtures.de_beauvoir import GOLDEN_LISTING, GOLDEN_SEARCH  # noqa: E402
from gaff_engine.fixtures.shortlist import demo_shortlist  # noqa: E402
from gaff_engine.schemas import Ref, ReportStat  # noqa: E402
from gaff_engine.validate import validate  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PERSON = person_from_profile(json.load(open(os.path.join(HERE, "profile.json"))))
PREF = Ref(id="person_finn", schemaVersion="person@1")
SREF = Ref(id=GOLDEN_SEARCH.id, schemaVersion="search@1")


def _scored():
    scored = [(GOLDEN_LISTING, score(GOLDEN_LISTING, _PERSON, GOLDEN_SEARCH,
                                     taste_model=canonical_model(), forensics_model=canonical_forensics()))]
    for d in demo_shortlist():
        L = d["listing"]
        scored.append((L, score(L, _PERSON, GOLDEN_SEARCH,
                                taste_model=d["taste_model"], forensics_model=d["forensics_model"])))
    return scored


# ---------------------------------------------------------------------------
# §5.2 — the market report is sourced, and honest.
# ---------------------------------------------------------------------------

def test_report_assembles_and_every_stat_is_sourced():
    rep = market_report(GOLDEN_SEARCH, _scored())
    assert validate(rep) == [], validate(rep)
    assert report_lint(rep) == []                    # A4: every stat/evidence/headline sourced
    kinds = [s.kind for s in rep.sections]
    assert "value_landscape" in kinds and "supply_liquidity" in kinds and "risk_landscape" in kinds
    assert rep.headline["sources"]                   # the headline cites
    # the numbers are real: median DOM across 47/55/34/90 = 51; the short-lease count is surfaced
    supply = next(s for s in rep.sections if s.kind == "supply_liquidity")
    assert next(st for st in supply.stats if "days on market" in st.label.lower()).value == 51
    risk = next(s for s in rep.sections if s.kind == "risk_landscape")
    assert "1 of 4" in next(st.value for st in risk.stats if "short-lease" in st.label.lower())


def test_lint_rejects_an_unsourced_stat():
    rep = market_report(GOLDEN_SEARCH, _scored())
    rep.sections[0].stats.append(ReportStat(label="Made-up figure", value=99, sources=None))
    viol = report_lint(rep)
    assert any("Made-up figure" in v for v in viol)  # the linter catches it


def test_report_is_data_not_advice():
    rep = market_report(GOLDEN_SEARCH, _scored())
    text = (rep.headline["text"] + " " + " ".join(s.body for s in rep.sections)).lower()
    for banned in ("we recommend", "you should buy", "you should sell", "a great investment for you"):
        assert banned not in text                    # A5: presents the market, never personalised advice


def test_confidence_is_honest_and_caps_when_thin():
    full = market_report(GOLDEN_SEARCH, _scored())
    assert full.confidence == 0.72                    # 28 comps referenced → the spec's worked confidence
    thin = market_report(GOLDEN_SEARCH, _scored()[:1])  # one listing, 7 comps
    assert thin.confidence <= 0.6                      # thin coverage (<20 comps) honestly capped


# ---------------------------------------------------------------------------
# §5.3 — the docpack readiness + the hard guardrails.
# ---------------------------------------------------------------------------

def test_buy_mortgaged_readiness_is_computed_from_the_item_set():
    provided = {"photo_id": "vault_passport_ref", "proof_of_address": "vault_addr_ref",
                "solicitor_details": "vault_solicitor_ref"}
    pack = assemble_docpack(PREF, SREF, "buy_mortgaged", provided=provided)
    assert validate(pack) == [], validate(pack)
    # 6 required (the memo is optional), 3 provided → 50% (§5.3b), the three funds items missing (A7)
    assert pack.readiness["requiredTotal"] == 6 and pack.readiness["requiredProvided"] == 3
    assert pack.readiness["pct"] == 50
    assert set(pack.readiness["missing"]) == {"mortgage_agreement_in_principle", "proof_of_deposit", "source_of_funds"}


def test_gaff_generated_memo_is_provided_by_generation():
    pack = assemble_docpack(PREF, SREF, "buy_mortgaged")
    memo = next(it for it in pack.items if it.code == "memorandum_of_sale_contact")
    assert memo.source == "gaff_generated" and memo.status == "provided" and memo.required is False
    assert pack.generated and pack.generated[0].kind == "cover_memo"


def test_guardrails_no_secret_values_and_default_private():
    provided = {"photo_id": "vault_passport_ref", "proof_of_deposit": "vault_deposit_ref"}
    pack = assemble_docpack(PREF, SREF, "buy_mortgaged", provided=provided)
    # A6: stores vault references, never the document's secret contents
    assert holds_no_secret_values(pack) is True
    assert all((it.fileRef is None) or str(it.fileRef).startswith("vault_") for it in pack.items)
    assert pack.privacy["sensitivity"] == "high" and pack.privacy["exportable"] is True
    assert pack.sharePolicy["default"] == "private" and pack.sharePolicy["shares"] == []
    # funds/identity items are high-sensitivity
    assert next(it for it in pack.items if it.code == "proof_of_deposit").sensitivity == "high"


def test_docpack_has_no_transact_surface():
    pack = assemble_docpack(PREF, SREF, "buy_mortgaged")
    # the contract carries no submit/send/transact affordance — the human sends it (A6)
    for banned in ("submit", "send", "transact", "offer", "pay"):
        assert not hasattr(pack, banned)
    # a raw secret leaking in as a fileRef is rejected by the guardrail
    bad = assemble_docpack(PREF, SREF, "buy_cash", provided={"proof_of_funds": "40128899221234"})
    assert holds_no_secret_values(bad) is False


def test_all_variants_are_well_formed():
    for variant in DOCPACK_CONFIG:
        mode = "rent" if variant.startswith("rent") else "buy"
        pack = assemble_docpack(PREF, SREF, variant)
        assert validate(pack) == [], (variant, validate(pack))
        assert pack.mode == mode and pack.readiness["requiredTotal"] >= 1


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
